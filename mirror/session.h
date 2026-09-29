// Talks the scrcpy server protocol (pinned to the system scrcpy version):
// pushes the server, opens the video + control sockets, decodes video with
// FFmpeg (VA-API when available) and forwards touch/keyboard input.
#pragma once

#include <QObject>
#include <QProcess>
#include <QPointer>
#include <QVideoSink>
#include <QVideoFrame>
#include <QMutex>
#include <atomic>
#include <thread>

class Session : public QObject {
  Q_OBJECT
  Q_PROPERTY(QVideoSink *videoSink READ videoSink WRITE setVideoSink NOTIFY videoSinkChanged)
  Q_PROPERTY(QString deviceName READ deviceName NOTIFY deviceNameChanged)
  Q_PROPERTY(int frameWidth READ frameWidth NOTIFY frameSizeChanged)
  Q_PROPERTY(int frameHeight READ frameHeight NOTIFY frameSizeChanged)
  Q_PROPERTY(QString state READ state NOTIFY stateChanged)
  Q_PROPERTY(QString error READ error NOTIFY stateChanged)
  Q_PROPERTY(QString decoder READ decoder NOTIFY decoderChanged)
  Q_PROPERTY(bool screenOn READ screenOn NOTIFY screenOnChanged)

public:
  struct Options {
    QString serial;
    QString codec = "h265";
    int bitRate = 24000000;
    int maxFps = 120;
    int maxSize = 0;
    QString newDisplay;      // "WxH/dpi" for a virtual display (DeX / app window)
    bool flexDisplay = false;
    QString startApp;
    bool screenOff = false;  // turn the phone panel off while mirroring
    bool stayAwake = true;
    bool clipboardSync = true;  // phone copies land on the desktop clipboard
  };

  explicit Session(const Options &opts, QObject *parent = nullptr);
  ~Session() override;

  QVideoSink *videoSink() const { return m_sink; }
  void setVideoSink(QVideoSink *s);
  QString deviceName() const { return m_deviceName; }
  int frameWidth() const { return m_w; }
  int frameHeight() const { return m_h; }
  QString state() const { return m_state; }
  QString error() const { return m_error; }
  QString decoder() const { return m_decoder; }
  bool screenOn() const { return m_screenOn; }

  Q_INVOKABLE void start();
  Q_INVOKABLE void stop();
  // Reconnect (e.g. USB unplugged, phone now on Wi-Fi) with a new serial.
  void restart(const QString &serial);
  QString serial() const { return m_opts.serial; }

  // action: 0 down, 1 up, 2 move. x/y in video pixels.
  Q_INVOKABLE void touch(int action, double x, double y);
  Q_INVOKABLE void scroll(double x, double y, double h, double v);
  Q_INVOKABLE void key(int nativeScanCode, bool down);
  Q_INVOKABLE void releaseKeys();
  Q_INVOKABLE void keycode(int androidKeycode);  // down + up
  Q_INVOKABLE void backOrScreenOn();
  Q_INVOKABLE void expandNotifications();
  Q_INVOKABLE void expandSettings();
  Q_INVOKABLE void collapsePanels();
  Q_INVOKABLE void rotate();
  Q_INVOKABLE void setScreenPower(bool on);
  Q_INVOKABLE void startApp(const QString &name);
  Q_INVOKABLE void resizeDisplay(int w, int h);
  Q_INVOKABLE void pushClipboard(bool paste);

signals:
  void videoSinkChanged();
  void deviceNameChanged();
  void frameSizeChanged();
  void stateChanged();
  void decoderChanged();
  void screenOnChanged();
  void finished();

private:
  void setState(const QString &s, const QString &err = QString());
  void videoLoop();
  void controlLoop();
  bool connectSockets();
  void send(const QByteArray &msg);
  void presentFrame(const QVideoFrame &f);
  void sendKeyboardReport();
  void createKeyboard();

  Options m_opts;
  QPointer<QVideoSink> m_sink;
  QString m_deviceName, m_state = "idle", m_error, m_decoder;
  int m_w = 0, m_h = 0;
  bool m_screenOn = true;

  QString m_version;
  quint32 m_scid = 0;
  int m_port = 0;
  int m_videoFd = -1, m_controlFd = -1;
  std::thread m_videoThread, m_controlThread;
  std::atomic<bool> m_stopping{false};
  QMutex m_sendLock;

  QMutex m_frameLock;
  QVideoFrame m_pending;
  std::atomic<bool> m_framePosted{false};

  bool m_keyboardReady = false;
  bool m_keys[0x66] = {};
  quint8 m_mods = 0;
  qint64 m_clipSeq = 1;
};
