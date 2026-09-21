#include <QCommandLineParser>
#include <QGuiApplication>
#include <QProcess>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QStandardPaths>
#include <QDir>
#include <QFile>
#include <QTimer>

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
    QProcess::startDetached("hyprctl", {"dispatch", QString("hl.dsp.window.resize({ x = %1, y = %2, window = \"pid:%3\" })")
                                                        .arg(w).arg(h).arg(QCoreApplication::applicationPid())});
  }
};

static QString firstDevice() {
  QProcess p;
  p.start("adb", {"devices"});
  p.waitForFinished(5000);
  const QStringList lines = QString::fromUtf8(p.readAll()).split('\n');
  for (const QString &l : lines.mid(1)) {
    const QStringList cols = l.simplified().split(' ');
    if (cols.size() >= 2 && cols[1] == "device") return cols[0];
  }
  return {};
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
      {"start-app", "Launch an app, e.g. com.samsung.android.messaging.", "app"},
      {"screen-off", "Turn the phone panel off while mirroring."},
      {"no-audio", "Don't forward phone audio."},
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
  o.startApp = cli.value("start-app");
  o.screenOff = cli.isSet("screen-off");

  Session session(o);

  QString skinDir = cli.value("skin");
  if (!skinDir.isEmpty() && !QDir(skinDir).exists())
    skinDir = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/taildroid/skins/" + skinDir;

  // Audio rides on a second, video-less scrcpy so the mirror stays light.
  QProcess audio;
  if (!cli.isSet("no-audio") && exportDir.isEmpty())
    audio.start("scrcpy", {"--serial", o.serial, "--no-video", "--no-control", "--no-window",
                           "--audio-buffer=40", "--window-title=taildroid-audio"});

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
  QObject::connect(&retry, &QTimer::timeout, &app, [&] {
    QString next = firstDevice();
    QProcess p;
    p.start("adb", {"devices"});
    p.waitForFinished(3000);
    if (QString::fromUtf8(p.readAll()).contains(session.serial() + "\tdevice")) next = session.serial();
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
