// Reads an Android emulator skin (Samsung publishes Galaxy skins in this
// format): a `layout` file with nested `name { key value }` blocks plus images.
#pragma once

#include <QDir>
#include <QFile>
#include <QRegularExpression>
#include <QUrl>
#include <QVariantMap>

namespace skin {

inline QVariantMap parseBlock(const QStringList &tok, int &i) {
  QVariantMap map;
  while (i < tok.size()) {
    const QString t = tok[i++];
    if (t == "}") break;
    if (i < tok.size() && tok[i] == "{") {
      ++i;
      map.insert(t, parseBlock(tok, i));
    } else if (i < tok.size()) {
      map.insert(t, tok[i++]);
    }
  }
  return map;
}

// Returns {} when there is no usable skin; the QML then draws its own frame.
inline QVariantMap load(const QString &dirPath) {
  if (dirPath.isEmpty()) return {};
  QDir dir(dirPath);
  QFile f(dir.filePath("layout"));
  if (!f.open(QIODevice::ReadOnly)) return {};
  QString text = QString::fromUtf8(f.readAll());
  text.replace(QRegularExpression("#[^\n]*"), " ");
  text.replace("{", " { ").replace("}", " } ");
  QStringList tok = text.split(QRegularExpression("\\s+"), Qt::SkipEmptyParts);
  int i = 0;
  const QVariantMap root = parseBlock(tok, i);

  const QVariantMap parts = root["parts"].toMap();
  const QVariantMap display = parts["device"].toMap()["display"].toMap();
  const QVariantMap layout = root["layouts"].toMap()["portrait"].toMap();
  QString partName = "portrait";
  int dx = 0, dy = 0;
  for (auto it = layout.begin(); it != layout.end(); ++it) {
    const QVariantMap part = it.value().toMap();
    if (part["name"] == "device") {
      dx = part["x"].toInt();
      dy = part["y"].toInt();
    } else if (part.contains("name") && part["name"] != "device") {
      partName = part["name"].toString();
    }
  }
  const QVariantMap bodyPart = parts[partName].toMap();
  const QString image = bodyPart["background"].toMap()["image"].toString();
  if (image.isEmpty() || display.isEmpty()) return {};

  return {
      {"image", QUrl::fromLocalFile(dir.filePath(image))},
      {"width", layout.value("width", 0).toInt()},
      {"height", layout.value("height", 0).toInt()},
      {"screenX", dx + display["x"].toInt()},
      {"screenY", dy + display["y"].toInt()},
      {"screenW", display["width"].toInt()},
      {"screenH", display["height"].toInt()},
  };
}

}  // namespace skin
