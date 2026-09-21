#include "session.h"

#include <QClipboard>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QRandomGenerator>
#include <QVideoFrameFormat>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
}

namespace {

constexpr quint32 CODEC_H264 = 0x68323634;
constexpr quint32 CODEC_H265 = 0x68323635;
constexpr quint32 CODEC_AV1 = 0x00617631;
constexpr quint64 FLAG_SESSION = 1ULL << 63;
constexpr quint64 FLAG_CONFIG = 1ULL << 62;
constexpr quint64 FLAG_KEY = 1ULL << 61;
constexpr quint16 HID_KEYBOARD_ID = 1;
constexpr const char *SERVER_PATH = "/data/local/tmp/taildroid-server.jar";

// Linux evdev keycode -> HID usage (keyboard page). 0 = unmapped.
quint8 evdevToHid(int ev) {
  static const quint8 table[128] = {
      0,    0x29, 0x1e, 0x1f, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2d, 0x2e, 0x2a, 0x2b,  // 0-15
      0x14, 0x1a, 0x08, 0x15, 0x17, 0x1c, 0x18, 0x0c, 0x12, 0x13, 0x2f, 0x30, 0x28, 0,    0x04, 0x16,  // 16-31
      0x07, 0x09, 0x0a, 0x0b, 0x0d, 0x0e, 0x0f, 0x33, 0x34, 0x35, 0,    0x31, 0x1d, 0x1b, 0x06, 0x19,  // 32-47
      0x05, 0x11, 0x10, 0x36, 0x37, 0x38, 0,    0x55, 0,    0x2c, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e,  // 48-63
      0x3f, 0x40, 0x41, 0x42, 0x43, 0x53, 0x47, 0x5f, 0x60, 0x61, 0x56, 0x5c, 0x5d, 0x5e, 0x57, 0x59,  // 64-79
      0x5a, 0x5b, 0x62, 0x63, 0,    0,    0x64, 0x44, 0x45, 0,    0,    0,    0,    0,    0,    0,     // 80-95
      0x58, 0,    0x54, 0x46, 0,    0,    0x4a, 0x52, 0x4b, 0x50, 0x4f, 0x4d, 0x51, 0x4e, 0x49, 0x4c,  // 96-111
      0,    0,    0,    0,    0,    0,    0,    0x48, 0,    0,    0,    0,    0,    0,    0,    0x65,  // 112-127
  };
  return (ev >= 0 && ev < 128) ? table[ev] : 0;
}

quint8 evdevToModBit(int ev) {
  switch (ev) {
  case 29: return 1 << 0;   // left ctrl
  case 42: return 1 << 1;   // left shift
  case 56: return 1 << 2;   // left alt
  case 125: return 1 << 3;  // left meta
  case 97: return 1 << 4;   // right ctrl
  case 54: return 1 << 5;   // right shift
  case 100: return 1 << 6;  // right alt
  case 126: return 1 << 7;  // right meta
  }
  return 0;
}

const quint8 KEYBOARD_DESC[] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x75, 0x08, 0x95, 0x01, 0x81, 0x01, 0x05, 0x08, 0x19, 0x01,
    0x29, 0x05, 0x75, 0x01, 0x95, 0x05, 0x91, 0x02, 0x75, 0x03, 0x95, 0x01, 0x91, 0x01, 0x05, 0x07,
    0x19, 0x00, 0x29, 0x65, 0x15, 0x00, 0x25, 0x65, 0x75, 0x08, 0x95, 0x06, 0x81, 0x00, 0xC0,
};

void put8(QByteArray &b, quint8 v) { b.append(char(v)); }
void put16(QByteArray &b, quint16 v) { put8(b, v >> 8); put8(b, v); }
void put32(QByteArray &b, quint32 v) { put16(b, v >> 16); put16(b, v); }
void put64(QByteArray &b, quint64 v) { put32(b, v >> 32); put32(b, quint32(v)); }

bool readFully(int fd, void *buf, size_t len) {
  auto *p = static_cast<char *>(buf);
  while (len) {
    ssize_t r = ::recv(fd, p, len, 0);
    if (r <= 0) return false;
    p += r;
    len -= size_t(r);
  }
  return true;
}

quint32 be32(const quint8 *p) { return (quint32(p[0]) << 24) | (p[1] << 16) | (p[2] << 8) | p[3]; }
quint64 be64(const quint8 *p) { return (quint64(be32(p)) << 32) | be32(p + 4); }

QString run(const QStringList &args, int timeoutMs = 15000, int *exitCode = nullptr) {
  QProcess p;
  p.setProcessChannelMode(QProcess::MergedChannels);
  p.start(args.first(), args.mid(1));
  p.waitForFinished(timeoutMs);
  if (exitCode) *exitCode = p.exitStatus() == QProcess::NormalExit ? p.exitCode() : -1;
  return QString::fromUtf8(p.readAll()).trimmed();
}

// The AMD iGPU decodes HEVC/AV1 through VA-API without waking the dGPU.
QString vaapiNode() {
  QDir dri("/sys/class/drm");
  QString fallback;
  for (const QString &n : dri.entryList({"renderD*"})) {
    QString driver = QFile::symLinkTarget(dri.filePath(n + "/device/driver"));
    if (driver.endsWith("/amdgpu") || driver.endsWith("/i915") || driver.endsWith("/xe")) return "/dev/dri/" + n;
    if (fallback.isEmpty() && !driver.endsWith("/nvidia")) fallback = "/dev/dri/" + n;
  }
  return fallback;
}

AVPixelFormat pickHw(AVCodecContext *, const AVPixelFormat *fmts) {
  for (const AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; ++p)
    if (*p == AV_PIX_FMT_VAAPI) return *p;
  return fmts[0];
}

}  // namespace

Session::Session(const Options &opts, QObject *parent) : QObject(parent), m_opts(opts) {}

Session::~Session() { stop(); }

void Session::setVideoSink(QVideoSink *s) {
  if (m_sink == s) return;
  m_sink = s;
  emit videoSinkChanged();
}

void Session::setState(const QString &s, const QString &err) {
  QMetaObject::invokeMethod(this, [this, s, err] {
    m_state = s;
    m_error = err;
    emit stateChanged();
    if (s == "closed" || s == "error") emit finished();
  }, Qt::QueuedConnection);
}

void Session::start() {
  if (m_videoThread.joinable()) return;
  m_stopping = false;
  m_videoThread = std::thread([this] { videoLoop(); });
}

void Session::stop() {
  if (m_stopping.exchange(true)) return;
  if (m_videoFd >= 0) ::shutdown(m_videoFd, SHUT_RDWR);
  if (m_controlFd >= 0) ::shutdown(m_controlFd, SHUT_RDWR);
  if (m_videoThread.joinable()) m_videoThread.join();
  if (m_controlThread.joinable()) m_controlThread.join();
  if (m_videoFd >= 0) ::close(m_videoFd);
  if (m_controlFd >= 0) ::close(m_controlFd);
  m_videoFd = m_controlFd = -1;
}

void Session::restart(const QString &serial) {
  stop();
  m_opts.serial = serial;
  m_keyboardReady = false;
  memset(m_keys, 0, sizeof m_keys);
  m_mods = 0;
  start();
}

bool Session::connectSockets() {
  auto connectOne = [this]() -> int {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(quint16(m_port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof addr) < 0) {
      ::close(fd);
      return -1;
    }
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    return fd;
  };

  // adb accepts the forward at once; the dummy byte proves the server listens.
  for (int attempt = 0; attempt < 100 && !m_stopping; ++attempt) {
    int fd = connectOne();
    char dummy;
    if (fd >= 0 && readFully(fd, &dummy, 1)) {
      m_videoFd = fd;
      break;
    }
    if (fd >= 0) ::close(fd);
    ::usleep(100 * 1000);
  }
  if (m_videoFd < 0) return false;
  m_controlFd = connectOne();
  return m_controlFd >= 0;
}

void Session::videoLoop() {
  setState("connecting");
  const QString adb = "adb";
  QStringList s = {adb, "-s", m_opts.serial};

  m_version = run({"scrcpy", "--version"}).section(' ', 1, 1);
  if (m_version.isEmpty()) return setState("error", "scrcpy is not installed");

  int code = 0;
  QString out = run(s + QStringList{"push", "/usr/share/scrcpy/scrcpy-server", SERVER_PATH}, 30000, &code);
  if (code != 0) return setState("error", out.isEmpty() ? "adb push failed" : out.section('\n', -1));

  m_scid = QRandomGenerator::global()->generate() & 0x7fffffff;
  const QString socketName = QString("localabstract:scrcpy_%1").arg(m_scid, 8, 16, QChar('0'));
  m_port = run(s + QStringList{"forward", "tcp:0", socketName}).toInt();
  if (m_port <= 0) return setState("error", "adb forward failed");

  QStringList serverArgs = {
      QString("CLASSPATH=%1").arg(SERVER_PATH), "app_process", "/", "com.genymobile.scrcpy.Server", m_version,
      QString("scid=%1").arg(m_scid, 8, 16, QChar('0')), "log_level=info", "audio=false", "tunnel_forward=true",
      "video_codec=" + m_opts.codec, QString("video_bit_rate=%1").arg(m_opts.bitRate),
      QString("max_fps=%1").arg(m_opts.maxFps), QString("max_size=%1").arg(m_opts.maxSize),
      QString("stay_awake=%1").arg(m_opts.stayAwake ? "true" : "false"), "clipboard_autosync=true",
      "power_off_on_close=false"};
  if (!m_opts.newDisplay.isEmpty()) serverArgs << "new_display=" + m_opts.newDisplay;
  if (m_opts.flexDisplay) serverArgs << "flex_display=true";

  QByteArrayList argv8;
  for (const QString &a : s + QStringList{"shell"} + serverArgs) argv8 << a.toUtf8();
  std::vector<char *> argv;
  for (QByteArray &a : argv8) argv.push_back(a.data());
  argv.push_back(nullptr);
  pid_t serverPid = ::fork();
  if (serverPid == 0) {
    ::setpgid(0, 0);
    int devnull = ::open("/dev/null", O_WRONLY);
    ::dup2(devnull, 1);
    ::execvp(argv[0], argv.data());
    ::_exit(127);
  }

  auto cleanup = [&] {
    if (serverPid > 0) {
      ::kill(-serverPid, SIGTERM);
      ::waitpid(serverPid, nullptr, 0);
    }
    run(s + QStringList{"forward", "--remove", QString("tcp:%1").arg(m_port)});
  };

  if (!connectSockets()) {
    cleanup();
    return setState(m_stopping ? "closed" : "error", "Phone did not start the mirror server");
  }

  char name[64] = {};
  quint8 hdr[12];
  if (!readFully(m_videoFd, name, 64) || !readFully(m_videoFd, hdr, 4)) {
    cleanup();
    return setState("error", "Lost connection during handshake");
  }
  QString deviceName = QString::fromUtf8(name);
  QMetaObject::invokeMethod(this, [this, deviceName] { m_deviceName = deviceName; emit deviceNameChanged(); });

  const quint32 codecId = be32(hdr);
  if (codecId <= 1) {
    cleanup();
    if (m_opts.codec != "h264" && !m_stopping) {
      // This phone's encoder can't do the requested codec; H.264 always works.
      m_opts.codec = "h264";
      ::close(m_videoFd);
      ::close(m_controlFd);
      m_videoFd = m_controlFd = -1;
      return videoLoop();
    }
    return setState("error", "Phone refused the video stream");
  }

  const AVCodecID avId = codecId == CODEC_H265 ? AV_CODEC_ID_HEVC : codecId == CODEC_AV1 ? AV_CODEC_ID_AV1 : AV_CODEC_ID_H264;
  const AVCodec *codec = avcodec_find_decoder(avId);
  AVCodecContext *ctx = avcodec_alloc_context3(codec);
  ctx->flags |= AV_CODEC_FLAG_LOW_DELAY;
  AVBufferRef *hwDevice = nullptr;
  QString node = vaapiNode();
  QString decoderName = QString("%1 (software)").arg(codec->name);
  if (!node.isEmpty() && av_hwdevice_ctx_create(&hwDevice, AV_HWDEVICE_TYPE_VAAPI, node.toUtf8().constData(), nullptr, 0) == 0) {
    ctx->hw_device_ctx = av_buffer_ref(hwDevice);
    ctx->get_format = pickHw;
    decoderName = QString("%1 (VA-API %2)").arg(codec->name, node.section('/', -1));
  } else {
    ctx->thread_count = 4;
    ctx->thread_type = FF_THREAD_SLICE;
  }
  if (avcodec_open2(ctx, codec, nullptr) < 0) {
    cleanup();
    return setState("error", "Could not open the video decoder");
  }
  QMetaObject::invokeMethod(this, [this, decoderName] { m_decoder = decoderName; emit decoderChanged(); });

  m_controlThread = std::thread([this] { controlLoop(); });
  QMetaObject::invokeMethod(this, [this] {
    createKeyboard();
    if (m_opts.screenOff) setScreenPower(false);
    if (!m_opts.startApp.isEmpty()) startApp(m_opts.startApp);
  });
  setState("streaming");

  AVPacket *pkt = av_packet_alloc();
  AVFrame *frame = av_frame_alloc();
  AVFrame *sw = av_frame_alloc();
  SwsContext *sws = nullptr;
  QByteArray config;

  while (!m_stopping) {
    if (!readFully(m_videoFd, hdr, 12)) break;
    const quint64 ptsFlags = be64(hdr);
    if (ptsFlags & FLAG_SESSION) continue;  // width/height arrive with the next decoded frame
    const quint32 len = be32(hdr + 8);
    QByteArray data(int(len), Qt::Uninitialized);
    if (!readFully(m_videoFd, data.data(), len)) break;
    if (ptsFlags & FLAG_CONFIG) {
      config = data;
      continue;
    }
    if (!config.isEmpty()) {
      data.prepend(config);
      config.clear();
    }
    av_new_packet(pkt, data.size());
    memcpy(pkt->data, data.constData(), size_t(data.size()));
    pkt->pts = qint64(ptsFlags & ~(FLAG_SESSION | FLAG_CONFIG | FLAG_KEY));
    if (ptsFlags & FLAG_KEY) pkt->flags |= AV_PKT_FLAG_KEY;
    int ret = avcodec_send_packet(ctx, pkt);
    av_packet_unref(pkt);
    if (ret < 0) continue;

    while (avcodec_receive_frame(ctx, frame) == 0) {
      AVFrame *src = frame;
      if (frame->format == AV_PIX_FMT_VAAPI) {
        av_frame_unref(sw);
        if (av_hwframe_transfer_data(sw, frame, 0) < 0) continue;
        src = sw;
      }
      const int w = src->width, h = src->height;
      QVideoFrameFormat::PixelFormat qfmt =
          src->format == AV_PIX_FMT_NV12 ? QVideoFrameFormat::Format_NV12 : QVideoFrameFormat::Format_YUV420P;
      QVideoFrame qf(QVideoFrameFormat(QSize(w, h), qfmt));
      if (!qf.map(QVideoFrame::WriteOnly)) continue;

      if (src->format == AV_PIX_FMT_NV12 || src->format == AV_PIX_FMT_YUV420P || src->format == AV_PIX_FMT_YUVJ420P) {
        const int planes = src->format == AV_PIX_FMT_NV12 ? 2 : 3;
        for (int p = 0; p < planes; ++p) {
          const int rows = p == 0 ? h : (h + 1) / 2;
          const int bytes = std::min(qf.bytesPerLine(p), std::abs(src->linesize[p]));
          for (int r = 0; r < rows; ++r)
            memcpy(qf.bits(p) + r * qf.bytesPerLine(p), src->data[p] + r * src->linesize[p], size_t(bytes));
        }
      } else {
        // 10-bit or unusual layouts: convert once to planar 8-bit.
        sws = sws_getCachedContext(sws, w, h, AVPixelFormat(src->format), w, h, AV_PIX_FMT_YUV420P,
                                   SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
        uint8_t *dst[3] = {qf.bits(0), qf.bits(1), qf.bits(2)};
        int dstStride[3] = {qf.bytesPerLine(0), qf.bytesPerLine(1), qf.bytesPerLine(2)};
        sws_scale(sws, src->data, src->linesize, 0, h, dst, dstStride);
      }
      qf.unmap();
      presentFrame(qf);
    }
  }

  sws_freeContext(sws);
  av_frame_free(&frame);
  av_frame_free(&sw);
  av_packet_free(&pkt);
  avcodec_free_context(&ctx);
  av_buffer_unref(&hwDevice);
  if (m_controlFd >= 0) ::shutdown(m_controlFd, SHUT_RDWR);
  cleanup();
  setState(m_stopping ? "closed" : "error", m_stopping ? QString() : "Phone disconnected");
}

void Session::presentFrame(const QVideoFrame &f) {
  {
    QMutexLocker l(&m_frameLock);
    m_pending = f;
  }
  // Coalesce: if the UI hasn't consumed the last frame yet, only the newest wins.
  if (m_framePosted.exchange(true)) return;
  QMetaObject::invokeMethod(this, [this] {
    QVideoFrame f;
    {
      QMutexLocker l(&m_frameLock);
      f = m_pending;
      m_pending = QVideoFrame();
    }
    m_framePosted = false;
    if (!f.isValid()) return;
    if (f.width() != m_w || f.height() != m_h) {
      m_w = f.width();
      m_h = f.height();
      emit frameSizeChanged();
    }
    if (m_sink) m_sink->setVideoFrame(f);
  }, Qt::QueuedConnection);
}

void Session::controlLoop() {
  quint8 type;
  while (!m_stopping && readFully(m_controlFd, &type, 1)) {
    if (type == 0) {  // clipboard from the phone
      quint8 lenBuf[4];
      if (!readFully(m_controlFd, lenBuf, 4)) break;
      QByteArray text(int(be32(lenBuf)), Qt::Uninitialized);
      if (!readFully(m_controlFd, text.data(), size_t(text.size()))) break;
      QMetaObject::invokeMethod(this, [text] { QGuiApplication::clipboard()->setText(QString::fromUtf8(text)); });
    } else if (type == 1) {  // clipboard ack
      quint8 seq[8];
      if (!readFully(m_controlFd, seq, 8)) break;
    } else if (type == 2) {  // uhid output (keyboard LEDs); ignored
      quint8 h[4];
      if (!readFully(m_controlFd, h, 4)) break;
      QByteArray data((h[2] << 8) | h[3], Qt::Uninitialized);
      if (!readFully(m_controlFd, data.data(), size_t(data.size()))) break;
    } else {
      break;
    }
  }
}

void Session::send(const QByteArray &msg) {
  QMutexLocker l(&m_sendLock);
  if (m_controlFd < 0) return;
  const char *p = msg.constData();
  qsizetype left = msg.size();
  while (left > 0) {
    ssize_t w = ::send(m_controlFd, p, size_t(left), MSG_NOSIGNAL);
    if (w <= 0) return;
    p += w;
    left -= w;
  }
}

void Session::touch(int action, double x, double y) {
  if (m_w <= 0) return;
  QByteArray b;
  put8(b, 2);
  put8(b, quint8(action));
  put64(b, quint64(-2));  // generic finger: behaves like a real touch in every app
  put32(b, quint32(qint32(x)));
  put32(b, quint32(qint32(y)));
  put16(b, quint16(m_w));
  put16(b, quint16(m_h));
  put16(b, action == 1 ? 0 : 0xffff);
  put32(b, 0);
  put32(b, 0);
  send(b);
}

void Session::scroll(double x, double y, double h, double v) {
  if (m_w <= 0) return;
  auto fixed = [](double val) {
    double f = std::clamp(val / 16.0, -1.0, 1.0);
    return quint16(qint16(std::clamp(f * 0x8000, -32768.0, 32767.0)));
  };
  QByteArray b;
  put8(b, 3);
  put32(b, quint32(qint32(x)));
  put32(b, quint32(qint32(y)));
  put16(b, quint16(m_w));
  put16(b, quint16(m_h));
  put16(b, fixed(h));
  put16(b, fixed(v));
  put32(b, 0);
  send(b);
}

void Session::createKeyboard() {
  QByteArray b;
  put8(b, 12);
  put16(b, HID_KEYBOARD_ID);
  put16(b, 0);
  put16(b, 0);
  put8(b, 0);  // no name
  put16(b, sizeof KEYBOARD_DESC);
  b.append(reinterpret_cast<const char *>(KEYBOARD_DESC), sizeof KEYBOARD_DESC);
  send(b);
  m_keyboardReady = true;
}

void Session::sendKeyboardReport() {
  QByteArray report(8, 0);
  report[0] = char(m_mods);
  int n = 0;
  for (int i = 0; i < 0x66; ++i) {
    if (!m_keys[i]) continue;
    if (n == 6) {
      for (int k = 2; k < 8; ++k) report[k] = 1;  // rollover
      break;
    }
    report[2 + n++] = char(i);
  }
  QByteArray b;
  put8(b, 13);
  put16(b, HID_KEYBOARD_ID);
  put16(b, 8);
  b.append(report);
  send(b);
}

void Session::key(int nativeScanCode, bool down) {
  if (!m_keyboardReady) return;
  const int ev = nativeScanCode - 8;  // xkb keycode -> evdev
  if (quint8 bit = evdevToModBit(ev)) {
    m_mods = down ? (m_mods | bit) : (m_mods & ~bit);
  } else if (quint8 usage = evdevToHid(ev)) {
    // Ctrl+V pastes the desktop clipboard, like scrcpy does.
    if (down && usage == 0x19 && (m_mods & 0x11)) pushClipboard(false);
    m_keys[usage] = down;
  } else {
    return;
  }
  sendKeyboardReport();
}

void Session::releaseKeys() {
  if (!m_keyboardReady) return;
  memset(m_keys, 0, sizeof m_keys);
  m_mods = 0;
  sendKeyboardReport();
}

void Session::keycode(int androidKeycode) {
  for (quint8 action : {0, 1}) {
    QByteArray b;
    put8(b, 0);
    put8(b, action);
    put32(b, quint32(androidKeycode));
    put32(b, 0);
    put32(b, 0);
    send(b);
  }
}

void Session::backOrScreenOn() {
  for (quint8 action : {0, 1}) {
    QByteArray b;
    put8(b, 4);
    put8(b, action);
    send(b);
  }
}

void Session::expandNotifications() { send(QByteArray(1, 5)); }
void Session::expandSettings() { send(QByteArray(1, 6)); }
void Session::collapsePanels() { send(QByteArray(1, 7)); }
void Session::rotate() { send(QByteArray(1, 11)); }

void Session::setScreenPower(bool on) {
  QByteArray b;
  put8(b, 10);
  put8(b, on ? 1 : 0);
  send(b);
  m_screenOn = on;
  emit screenOnChanged();
}

void Session::startApp(const QString &name) {
  QByteArray n = name.toUtf8().left(255);
  QByteArray b;
  put8(b, 16);
  put8(b, quint8(n.size()));
  b.append(n);
  send(b);
}

void Session::resizeDisplay(int w, int h) {
  QByteArray b;
  put8(b, 21);
  put16(b, quint16(w));
  put16(b, quint16(h));
  send(b);
}

void Session::pushClipboard(bool paste) {
  QByteArray text = QGuiApplication::clipboard()->text().toUtf8();
  if (text.isEmpty()) return;
  QByteArray b;
  put8(b, 9);
  put64(b, quint64(m_clipSeq++));
  put8(b, paste ? 1 : 0);
  put32(b, quint32(text.size()));
  b.append(text);
  send(b);
}
