-- HERMES handheld client v2.3 for lpp-vita
-- v2.1 crash forensics: System.readFile(handle,size) does buffer[read]=0 with no
-- error check; a failed sceIoRead wrote buffer[0x8001000d] -> data abort.
-- v2.3 rules:
--   * readFile ONLY after openFile(READ_ONLY) succeeded and sizeFile returned a
--     sane n (proven-safe open/size sequence ran clean on real talk.wav in v2.2).
--   * No FTP dependency at runtime: homebrew apps are exclusive, molecularShell
--     FTP dies when this app launches. Bytes go inline over the socket via the
--     C/T protocol already verified against the bridge (R 99884 round trips).
--   * Mic.stop is synchronous (source writes whole WAV + sceIoClose before
--     returning), so the file is fully closed before we open it.
-- v2.4 (source-verified, luaNetwork.cpp):
--   * lpp sets SO_NBIO on every socket: sceNetSend returns EBLOCK/EWOULDBLOCK
--     (negative) once the send buffer drains — NOT a fatal error. sendall now
--     chunks to 8KB and retries negatives with System.wait, bounded deadline.
--   * Socket.receive returns "" for both "no data yet" and closed: receivers
--     must pace misses with System.wait so the ~20s STT+brain+TTS latency does
--     not expire the read window.
-- v2.14-uiux: real interface - colored transcript log (her=yellow, err=red,
--   sys=gray, cam=green), header status bar (state dot, recording pulse,
--   thinking progress bar, speaking equalizer), footer key legend, CAM chip,
--   on-screen reply text (bridge sends "R len tlen" + audio + text; tlen
--   optional so an old bridge still works).
-- X talk | R rear cam | O cancel/stop | up/down scroll | START exit

local BRIDGE_IP   = "192.168.1.10"  -- EDIT ME: the machine running /vita-bridge start
local BRIDGE_PORT = 8643
local STEPLOG     = "ux0:/data/step.log"
local TALK_FILE   = "ux0:/data/talk.wav"
local CAP_FILE    = "ux0:/data/cap.png"
local REPLY_FILE  = "ux0:/data/reply.wav"
local MAX_WAV     = 250000
local MAX_PNG     = 400000

pcall(Sound.init)
pcall(Network.init)

-- v2.15 assets ship inside the VPK (app0:/). Face overlays are drawn at
-- coordinates measured from avatar_src.png by facebox.swift:
--   face bbox x:82..242 y:148..391 in the 403x512 original
--   => scaled to h=510: face cx~161 top~147 h~241
local bg_img    = nil
local avatar    = nil
local av_blink, av_talk = nil, nil
do
  local okb, im = pcall(Graphics.loadImage, "app0:/bg.png")
  if okb and im then bg_img = im end
  local oka, im2 = pcall(Graphics.loadImage, "app0:/avatar.png")
  if oka and im2 then avatar = im2 end
  local function ld(path)
    local ok, i = pcall(Graphics.loadImage, path)
    if ok and i then return i end
    return nil
  end
  av_blink = ld("app0:/blink.png")
  av_talk  = ld("app0:/talk.png")
end

local black  = Color.new(0,0,0)
local white  = Color.new(255,255,255)
local cyan   = Color.new(128,208,240)
local yellow = Color.new(248,248,248)
local red    = Color.new(255,80,80)
local bg     = Color.new(0,30,45)
local panel  = Color.new(0,56,80)
local gray   = Color.new(160,192,216)
local green  = Color.new(90,220,130)

local log    = {"HERMES VITA v2.21-MICFIX"}
local log_c  = {cyan, gray}
local scroll = 0
local state  = "IDLE"
local snd    = nil
local armed  = false
local preview = nil
local think_elapsed = 0
local frame  = 0
local stepn  = 0
local bubble = nil      -- latest reply text for the speech bubble
local blink  = 0        -- blink countdown
local nextbl = 90

local function push(s, c)
  table.insert(log, s)
  table.insert(log_c, c or gray)
  if #log > 60 then table.remove(log, 1); table.remove(log_c, 1) end
  scroll = math.max(0, #log - 30)
end

-- header/legend text is pseudo-bold (4 offset draws) so it reads at arm's
-- length on the small handheld panel
local function bp(t, x, y, c)
  Graphics.debugPrint(x, y, t, c)
  Graphics.debugPrint(x + 1, y, t, c)
  Graphics.debugPrint(x, y + 1, t, c)
  Graphics.debugPrint(x + 1, y + 1, t, c)
end

local function step(tag)
  stepn = stepn + 1
  local s = tostring(stepn) .. " " .. tag .. "\n"
  -- SCE_O_RDWR|SCE_O_CREAT|SCE_O_APPEND = 0x3|0x200|0x100 = 0x303
  -- (openFile RAISES on missing files, hence pcall; APPEND needs no seek)
  local ok, h = pcall(System.openFile, STEPLOG, 771)
  if ok and type(h) == "number" and h >= 0 then
    System.writeFile(h, s, #s)
    System.closeFile(h)
  end
end

-- guarded read: openFile(READ_ONLY) then sizeFile, both proven-safe primitives;
-- readFile only runs when the fd and size are known good.
local function readfile_guarded(path, maxn)
  local ok, h = pcall(System.openFile, path, READ_ONLY)
  if not ok or type(h) ~= "number" or h < 0 then return nil, "open" end
  local n = System.sizeFile(h)
  if type(n) ~= "number" or n <= 0 or n > maxn then
    System.closeFile(h)
    return nil, "size" .. tostring(n)
  end
  local data = System.readFile(h, n)
  System.closeFile(h)
  if type(data) ~= "string" or #data ~= n then return nil, "shortread" end
  return data, n
end

local function fsize(path)
  local ok, h = pcall(System.openFile, path, READ_ONLY)
  if not ok or type(h) ~= "number" or h < 0 then return 0 end
  local n = System.sizeFile(h)
  System.closeFile(h)
  if type(n) ~= "number" or n < 0 then return 0 end
  return n
end

local function writefile(path, data)
  -- SCE_O_RDWR|SCE_O_CREAT|SCE_O_TRUNC = 0x3|0x200|0x400 = 0x603
  -- openFile(READ_WRITE=3) alone RAISES on missing files; CREAT fixes that,
  -- TRUNC keeps reply.wav clean when replies change size.
  local ok, h = pcall(System.openFile, path, 1539)
  if not ok or type(h) ~= "number" or h < 0 then push("ERR: open " .. path); return false end
  System.writeFile(h, data, #data)
  System.closeFile(h)
  return true
end

-- chunked, non-blocking-safe AND pool-safe: sceNet pool is only 141KB
-- (NET_INIT_SIZE in luaNetwork.cpp); the un-paced 192KB upload exhausted it
-- and the link died at retransmit timeout ~8s (diag proved 2.4KB upload +
-- full 88KB reply works). Send 4KB chunks with 15ms drains in between.
local function sendall(skt, s)
  local off = 1
  local retries = 0
  while off <= #s do
    local chunk = s:sub(off, off + 4095)
    local r = Socket.send(skt, chunk)
    if type(r) == "number" and r > 0 then
      off = off + r
      retries = 0
      if off <= #s then System.wait(15000) end
    else
      retries = retries + 1
      if retries > 4000 then return false end
      System.wait(5000)
    end
  end
  return true
end

-- paced for non-blocking sockets: "" means "no data yet"; default budget 80s,
-- callers with short-lived connections pass a small budget (each miss = 10ms)
local function recvline(skt, budget)
  local line, miss = "", 0
  budget = budget or 8000
  while miss < budget do
    local c = Socket.receive(skt, 1)
    if c == "" then
      miss = miss + 1
      System.wait(10000)   -- 10ms; System.wait takes MICROSECONDS (lua_wait)
    else
      miss = 0
      if c == "\n" then return line end
      line = line .. c
    end
  end
  return line
end

local function recvn(skt, n, budget)
  local buf, miss = "", 0
  budget = budget or 8000
  while #buf < n and miss < budget do
    local d = Socket.receive(skt, math.min(60000, n - #buf))
    if d == "" then miss = miss + 1; System.wait(10000) else miss = 0; buf = buf .. d end
  end
  return buf
end

local function play_reply()
  if snd then Sound.close(snd); snd = nil end
  snd = Sound.open(REPLY_FILE)
  if type(snd) == "number" and snd ~= 0 then
    Sound.play(snd)
    state = "SPEAKING"
  else
    snd = nil
    state = "IDLE"
    push("ERR: reply.wav not playable")
  end
end

local session = 0

local function talk()
  think_elapsed = 0
  step("mic_start")
  state = "LISTENING"; push("[ rec ] listening 6s...", red)
  Mic.start(6, 16000)
  -- BISECT TEST v2.22: zero draw calls in the mic loop (pure wait, the
  -- v2.10/2.13 pattern that never crashed). One variable at a time.
  while Mic.isRecording() do System.wait(300000) end
  Mic.stop(TALK_FILE)
  step("mic_stop")

  local wav, werr = readfile_guarded(TALK_FILE, MAX_WAV)
  if not wav then state = "IDLE"; step("wavread_" .. werr); push("err: wav " .. werr, red); return end
  local png, plen = "", 0
  if armed then
    local p = readfile_guarded(CAP_FILE, MAX_PNG)
    if p then png = p; plen = #p end
    armed = false
  end
  step("read_" .. #wav .. "+" .. plen)
  push("[ sent ] voice " .. #wav .. "B" .. (plen > 0 and " + image " .. plen .. "B" or ""), gray)

  state = "THINKING"
  local skt = Socket.connect(BRIDGE_IP, BRIDGE_PORT)
  if type(skt) ~= "number" or skt == 0 then
    state = "IDLE"; step("connect_fail"); push("err: connect " .. BRIDGE_IP, red); return
  end
  step("connected")
  session = session + 1
  local sid = "v" .. tostring(session)
  sendall(skt, "U " .. sid .. "\n")
  sendall(skt, "C " .. tostring(#wav + plen) .. "\n")
  if not sendall(skt, wav) or (plen > 0 and not sendall(skt, png)) then
    Socket.close(skt); state = "IDLE"; step("send_fail"); push("err: upload failed", red); return
  end
  local ack = recvline(skt)
  step("ack_" .. ack)
  sendall(skt, "T " .. sid .. " " .. #wav .. " " .. plen .. "\n")
  step("sent_T")
  local go = recvline(skt, 500)
  Socket.close(skt)              -- lifetime ends here; never span the pipeline
  step("got_G_" .. go)
  if go ~= "G" then
    state = "IDLE"; push("err: expected G got [" .. go:sub(1, 20) .. "]", red); return
  end
  -- Poll on fresh short P-connections: W=still cooking, E=bridge error,
  -- R len + body. Budget 150x2s=300s: measured vision rounds run 90-95s
  -- end-to-end (STT+brain+TTS), so the window must clear several minutes.
  -- Circle aborts the round mid-wait.
  for try = 1, 150 do
    System.wait(2000000)   -- 2s between poll connections (microseconds)
    think_elapsed = try * 2
    local pad2 = Controls.read()
    if Controls.check(pad2, SCE_CTRL_CIRCLE) then
      step("cancel_p" .. try); push("[cancelled] after " .. think_elapsed .. "s", yellow); state = "IDLE"; return
    end
    local sk2 = Socket.connect(BRIDGE_IP, BRIDGE_PORT)
    if type(sk2) == "number" and sk2 ~= 0 then
      sendall(sk2, "P " .. sid .. "\n")
      local hdr = recvline(sk2, 300)
      step("p" .. try .. "_" .. hdr:gsub("%c", "."):sub(1, 14))
      if hdr == "W" then
        Socket.close(sk2)
      elseif hdr:sub(1, 1) == "E" then
        Socket.close(sk2)
        push("bridge: " .. hdr:sub(3, 60), red); state = "IDLE"; return
      else
        local rlen = tonumber(hdr:match("R (%d+)"))
        local tlen = tonumber(hdr:match("R %d+ (%d+)")) or 0
        if rlen and rlen >= 100 then
          local audio = recvn(sk2, rlen, 8000)
          step("recv_" .. #audio .. "_" .. rlen)
          if #audio == rlen and writefile(REPLY_FILE, audio) then
            local txt = ""
            if tlen > 0 then txt = recvn(sk2, tlen, 4000) end
            if txt ~= "" then bubble = txt end
            push("her: " .. txt:sub(1, 105), yellow)
            push("[ audio " .. rlen .. "B ]", gray)
            Socket.close(sk2)
            play_reply()
            return
          end
          push("err: short reply " .. #audio .. "/" .. rlen, red)
        else
          push("err: bridge ['" .. hdr:gsub("%c", "."):sub(1, 24) .. "']", red)
        end
        Socket.close(sk2)
        state = "IDLE"; return
      end
    else
      step("p" .. try .. "_connfail")
    end
  end
  push("[err] reply timeout (300s)", red); state = "IDLE"
end

local function wrap(t, w)
  local lines = {}
  local cur = ""
  for word in t:gmatch("%S+") do
    if #cur + #word + 1 > w then table.insert(lines, cur); cur = word
    else cur = (cur == "" and word) or (cur .. " " .. word) end
  end
  if cur ~= "" then table.insert(lines, cur) end
  return lines
end

local function render()
  -- PRIMITIVE SEMANTICS (lpp source/luaGraphics.cpp lua_rect/lua_emptyrect):
  --   fillRect(x1, x2, y1, y2, color)  <- NOTE: x2 SECOND, not y1.
  --   debugPrint(x, y, ...) draws glyph baseline at y+17.4 => text grid pitch must be 16.
  frame = frame + 1
  -- blink scheduler: 8-frame closed window, next blink in ~1-2.5s at ~12fps
  blink = blink - 1
  if blink < -8 then nextbl = 55 + math.floor(math.abs(math.sin(frame * 0.37)) * 100); blink = nextbl end
  Graphics.initBlend()
  Screen.clear(black)
  -- full-bleed background: bg.png is 512x286 -> fill 960x544
  if bg_img then
    pcall(Graphics.drawScaleImage, 0, 0, bg_img, 960 / 512, 544 / 286)
  else
    Screen.clear(bg)
  end
  -- portrait: 403x512 states drawn flush top-left at ~1.0 scale (occupies x0..401, y0..510)
  if avatar then
    local aimg = avatar
    local closed = blink > 0 and blink <= 8
    if closed and av_blink then
      aimg = av_blink
    elseif state == "SPEAKING" and av_talk and math.floor(frame / 4) % 2 == 0 then
      aimg = av_talk
    end
    pcall(Graphics.drawScaleImage, 0, 0, aimg, 510 / 512, 510 / 512)
    -- portrait edge accent: thin vertical bar at x400..403
    Graphics.fillRect(400, 403, 0, 510, Color.new(128, 208, 240, 110))
  end
  -- header status bar (right panel, y0..34)
  Graphics.fillRect(403, 960, 0, 34, Color.new(12, 14, 22, 200))
  local sc, stx = green, "IDLE"
  if state == "LISTENING" then sc, stx = red, "LISTENING"
  elseif state == "THINKING" then sc, stx = cyan, "THINKING " .. think_elapsed .. "s"
  elseif state == "SPEAKING" then sc, stx = green, "SPEAKING"
  elseif armed then sc, stx = green, "IDLE + cam armed" end
  bp(stx, 700, 8, sc)
  bp("HERMES", 415, 8, white)
  local rad = 6
  if state == "LISTENING" then rad = (frame % 4 < 2) and 4 or 7 end
  Graphics.fillCircle(935, 17, rad, sc)
  if state == "THINKING" then
    -- progress track + fill, x403..960 at y34..40
    Graphics.fillRect(403, 960, 34, 40, Color.new(30, 36, 52, 220))
    local w2 = math.floor(557 * math.min(think_elapsed / 120, 1))
    if w2 > 0 then Graphics.fillRect(403, 403 + w2, 34, 40, cyan) end
  end
  if state == "SPEAKING" then
    -- equalizer inside header: bars hang UP from y34, x500..611
    for i = 0, 5 do
      local hgt = 4 + math.floor(math.abs(math.sin((frame + i * 5) / 3.3)) * 18)
      Graphics.fillRect(500 + i * 18, 511 + i * 18, 34 - hgt, 34, green)
    end
  end
  local righttop = 40
  -- speech bubble: her latest reply (16px text grid)
  if bubble and state ~= "THINKING" then
    local lines = wrap(bubble, 64)
    local nl = math.min(#lines, 5)
    local bh = nl * 16 + 12
    Graphics.fillRect(408, 952, righttop, righttop + bh, Color.new(12, 14, 22, 205))
    Graphics.fillEmptyRect(408, 952, righttop, righttop + bh, yellow)
    for i = 1, nl do
      Graphics.debugPrint(418, righttop + 2 + (i - 1) * 16, lines[i], yellow)
    end
    righttop = righttop + bh + 8
  end
  -- camera preview replaces bubble area when armed (640x480 at 0.75 -> 480x360)
  if armed and preview then
    pcall(Graphics.drawScaleImage, 408, righttop, preview, 0.75, 0.75)
    Graphics.debugPrint(418, righttop + 362, "CAM ARMED - FRAME GOES WITH NEXT [X] TALK", cyan)
    righttop = righttop + 382
  end
  -- transcript log: 16px pitch, right panel, stops above footer
  local yy = righttop
  local start_i = math.max(1, math.min(math.max(0, #log - 24), scroll) + 1)
  for i = start_i, #log do
    if yy > 470 then break end
    Graphics.debugPrint(415, yy, log[i]:sub(1, 68), log_c[i] or gray)
    yy = yy + 16
  end
  if start_i > 1 then bp("...", 405, yy, white) end
  -- footer legend strip: full width, closes the screen (ONE legend, here only)
  Graphics.fillRect(0, 960, 512, 544, Color.new(12, 14, 22, 220))
  Graphics.debugPrint(415, 516, "X TALK   R CAM   O CANCEL/STOP   UP/DN SCROLL   START EXIT", gray)
  Graphics.termBlend()
  Screen.flip()
end

-- main loop
while true do
  local pad = Controls.read()
  if Controls.check(pad, SCE_CTRL_START) then
    step("exit")
    if snd then Sound.close(snd); snd = nil end
    if preview then pcall(Graphics.freeImage, preview); preview = nil end
    if bg_img then pcall(Graphics.freeImage, bg_img) end
    if avatar then pcall(Graphics.freeImage, avatar) end
    for _, im in {av_blink, av_talk} do
      if im then pcall(Graphics.freeImage, im) end
    end
    Sound.term()
    break
  end
  if Controls.check(pad, SCE_CTRL_UP)   then scroll = math.max(0, scroll - 1) end
  if Controls.check(pad, SCE_CTRL_DOWN) then scroll = math.min(math.max(0, #log - 24), scroll + 1) end

  if state == "IDLE" and Controls.check(pad, SCE_CTRL_CROSS) then
    local ok, err = pcall(talk)
    if not ok then
      state = "IDLE"
      push("err: " .. tostring(err):sub(1, 60), red)
      step("lua_err")
    end
  end
  if state == "IDLE" and Controls.check(pad, SCE_CTRL_RTRIGGER) then
    local ok, err = pcall(function()
      System.deleteFile(CAP_FILE)
      Camera.init(OUTER_CAM, VGA_RES, 30)
      -- Sensor warmup: first reads after start are unexposed (proven: every
      -- capture came back 640x480 at luminance <=2/255 — pure noise). Discard
      -- ~10 reads at ~100ms. NEVER freeImage a getOutput handle: it is lpp's
      -- static cam_buf; freeing it = use-after-free hard crash (v2.11 lesson).
      step("cam_warm")
      for w = 1, 10 do
        Camera.getOutput()
        System.wait(100000)
      end
      local img = Camera.getOutput()
      Graphics.saveImage(img, CAP_FILE, FORMAT_PNG)
      Camera.term()
      local sz = fsize(CAP_FILE)
      if sz < 5000 then error("tiny frame " .. sz) end
      armed = true
      if preview then pcall(Graphics.freeImage, preview); preview = nil end
      local okp, im = pcall(Graphics.loadImage, CAP_FILE)
      if okp and im then preview = im end
      push("cam armed " .. sz .. "B - send with [X]", green)
      step("cam_saved_" .. sz)
    end)
    if not ok then
      pcall(Camera.term)   -- release camera if init succeeded but later step failed
      push("err: cam " .. tostring(err):sub(1, 40), red)
      step("cam_err")
    end
  end
  if state == "SPEAKING" and Controls.check(pad, SCE_CTRL_CIRCLE) then
    if snd then Sound.close(snd); snd = nil end
    state = "IDLE"; push("[playback stopped]", gray)
  end
  if state == "SPEAKING" and not Sound.isPlaying(snd) then
    Sound.close(snd); snd = nil; state = "IDLE"
  end

  render()
  System.wait(80000)
end
