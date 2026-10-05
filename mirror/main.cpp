#include <QCommandLineParser>
#include <QGuiApplication>
#include <QProcess>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QStandardPaths>
#include <QDir>
#include <QFile>
#include <QTimer>

#include <csignal>
#include <sys/prctl.h>

#include "session.h"
#include "skin.h"

// Saves a built-in frame as an Android emulator skin (layout + PNG).
class Exporter : public QObject {
  Q_OBJECT
public:
  Q_INVOKABLE void writeLayout(const QString &dir, int w, int h, int sx, int sy, int sw, int sh) {
    QFile f(dir + "/layout");
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) return;
    f.write(QString("parts {\n  portrait {\n    background {\n      image device_Port.png\n    }\n  }\n"
                    "  device {\n    display {\n      width %1\n      height %2\n      x 0\n      y 0\n    }\n  }\n}\n\n"
                    "layouts {\n  portrait {\n    width %3\n    height %4\n    event EV_SW:0:1\n"
                    "    part1 {\n      name portrait\n      x 0\n      y 0\n    }\n"
                    "    part2 {\n      name device\n      x %5\n      y %6\n    }\n  }\n}\n")
                .arg(sw).arg(sh).arg(w).arg(h).arg(sx).arg(sy).toUtf8());
  }
};

// Hyprland ignores client resizes of floating windows; ask it directly.
class Hypr : public QObject {
  Q_OBJECT
public:
  Q_INVOKABLE void resize(int w, int h) {
    // Without `exact` this is a relative resize, which left the window at
    // whatever size Hyprland first gave it and the phone floating inside it.
    QProcess::startDetached("hyprctl", {"dispatch", QString("hl.dsp.window.resize({ x = %1, y = %2, exact = true, window = \"pid:%3\" })")
                                                        .arg(w).arg(h).arg(QCoreApplication::applicationPid())});
  }
};

// Ready serials from `adb devices` output, in listed order.
static QStringList readyDevices(const QByteArray &out) {
  QStringList ready;
  for (const QString &l : QString::fromUtf8(out).split('\n').mid(1)) {
    const QStringList cols = l.simplified().split(' ');
    if (cols.size() >= 2 && cols[1] == "device") ready << cols[0];
  }
  return ready;
}

static QString firstDevice() {
  QProcess p;
  p.start("adb", {"devices"});
  p.waitForFinished(5000);
  return readyDevices(p.readAll()).value(0);
}

int main(int argc, char **argv) {
  QGuiApplication app(argc, argv);
  app.setApplicationName("taildroid-mirror");
  app.setDesktopFileName("taildroid-mirror");  // Wayland app_id for Hyprland rules

  QCommandLineParser cli;
  cli.setApplicationDescription("Skinned Android mirror for Omarchy");
  cli.addHelpOption();
  const QList<QCommandLineOption> opts = {
      {{"s", "serial"}, "ADB serial.", "serial"},
      {"codec", "h265 | h264 | av1 (default h265).", "codec", "h265"},
      {"bitrate", "Video bit rate in Mbps (default 24).", "mbps", "24"},
      {"fps", "Max frame rate (default 120).", "fps", "120"},
      {"max-size", "Limit the longer side in pixels (0 = native).", "px", "0"},
      {"skin", "Android emulator skin folder (Samsung Galaxy skins).", "dir"},
      {"color", "Frame finish: onyx-black, marble-grey, cobalt-violet, amber-yellow, titanium-gray… or #hex.", "color", ""},
      {"frame", "s24 | s24-ultra | none (default s24).", "frame", "s24"},
      {"export-skin", "Save the built-in frame as a skin (layout + PNG) into this folder and exit.", "dir"},
      {"new-display", "Virtual display WxH/dpi (DeX, apps in their own window).", "spec"},
      {"flex", "Resize the virtual display with the window."},
      {"no-decorations", "Virtual display without the taskbar and system bars (one app)."},
      {"start-app", "Launch an app, e.g. com.samsung.android.messaging.", "app"},
      {"screen-off", "Turn the phone panel off while mirroring."},
      {"no-audio", "Don't forward phone audio."},
      {"no-clipboard", "Don't copy the phone's clipboard to the desktop (Ctrl+V still pastes)."},
      {"title", "Window title.", "title"},
  };
  cli.addOptions(opts);
  cli.process(app);

  const QString exportDir = cli.value("export-skin");
  if (!exportDir.isEmpty()) QDir().mkpath(exportDir);

  Session::Options o;
  o.serial = cli.isSet("serial") ? cli.value("serial") : exportDir.isEmpty() ? firstDevice() : "export";
  if (o.serial.isEmpty()) {
    qCritical("No authorized Android device on adb.");
    return 2;
  }
  o.codec = cli.value("codec");
  o.bitRate = cli.value("bitrate").toInt() * 1000000;
  o.maxFps = cli.value("fps").toInt();
  o.maxSize = cli.value("max-size").toInt();
  o.newDisplay = cli.value("new-display");
  o.flexDisplay = cli.isSet("flex");
  o.noDecorations = cli.isSet("no-decorations");
  o.startApp = cli.value("start-app");
  o.screenOff = cli.isSet("screen-off");
  o.clipboardSync = !cli.isSet("no-clipboard");

  Session session(o);

  QString skinDir = cli.value("skin");
  if (!skinDir.isEmpty() && !QDir(skinDir).exists())
    skinDir = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/taildroid/skins/" + skinDir;

  // Audio rides on a second, video-less scrcpy so the mirror stays light.
  QProcess audio;
  if (!cli.isSet("no-audio") && exportDir.isEmpty()) {
    // A killed mirror must not leave its audio forwarder behind: an orphan keeps
    // playing the phone through the laptop speakers with no window to close.
    audio.setChildProcessModifier([] { prctl(PR_SET_PDEATHSIG, SIGTERM); });
    // --audio-dup: without it scrcpy mutes the phone speaker and the laptop
    // plays everything, so YouTube in your hand comes out of the laptop.
    audio.start("scrcpy", {"--serial", o.serial, "--no-video", "--no-control", "--no-window",
                           "--audio-dup", "--audio-buffer=120", "--window-title=taildroid-audio"});
  }

  QQmlApplicationEngine engine;
  engine.rootContext()->setContextProperty("session", &session);
  engine.rootContext()->setContextProperty("skin", skin::load(skinDir));
  engine.rootContext()->setContextProperty("frameColor", cli.value("color"));
  engine.rootContext()->setContextProperty("frameStyle", cli.value("frame"));
  engine.rootContext()->setContextProperty("windowTitle", cli.value("title"));
  Hypr hypr;
  engine.rootContext()->setContextProperty("hypr", &hypr);
  Exporter exporter;
  engine.rootContext()->setContextProperty("exporter", &exporter);
  engine.rootContext()->setContextProperty("exportDir", exportDir.isEmpty() ? QString() : QDir(exportDir).absolutePath());
  // Like Continuity: a dropped link keeps the window and quietly reconnects,
  // following the phone from USB to Wi-Fi (or back) for up to two minutes.
  int retries = 0;
  QTimer retry;
  retry.setSingleShot(true);
  // One async `adb devices` per attempt: a hung adb server must not freeze the window.
  QProcess probe;
  QObject::connect(&retry, &QTimer::timeout, &app, [&] { probe.start("adb", {"devices"}); });
  QObject::connect(&probe, &QProcess::finished, &app, [&] {
    const QStringList ready = readyDevices(probe.readAll());
    const QString next = ready.contains(session.serial()) ? session.serial() : ready.value(0);
    if (next.isEmpty()) { if (++retries < 60) retry.start(2000); return; }
    session.restart(next);
  });
  QObject::connect(&session, &Session::finished, &app, [&] {
    if (session.state() == "closed") app.quit();
    else if (exportDir.isEmpty() && session.error() == "Phone disconnected" && retries < 60) { ++retries; retry.start(1500); }
  });
  QObject::connect(&session, &Session::stateChanged, &app, [&] { if (session.state() == "streaming") retries = 0; });
  engine.load(QUrl("qrc:/Main.qml"));
  if (engine.rootObjects().isEmpty()) return 1;

  if (exportDir.isEmpty()) session.start();
  const int rc = app.exec();
  session.stop();
  audio.terminate();
  audio.waitForFinished(1500);
  return rc;
}

#include "main.moc"
