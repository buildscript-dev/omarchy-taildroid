// Fast screen reader for the MCP server: `uiautomator dump`, kept connected.
//
// `uiautomator dump` spends ~2 s per call: a fresh VM, a new accessibility
// connection, and waitForIdle(1000 ms of quiet). This runs the phone's own
// uiautomator.jar classes (so the XML is byte-for-byte the same format) behind
// one long-lived connection, answering one dump per stdin line.
//
//   CLASSPATH=/system/framework/uiautomator.jar:/data/local/tmp/taildroid-ui.jar \
//     app_process / TaildroidUi
//
// Hidden classes are reached by reflection, so it compiles against android.jar.
// Build (writes ../taildroid-ui.jar, which the MCP server pushes on first use):
//   A=~/Android/sdk; B=$(mktemp -d)
//   javac --release 11 -cp $A/platforms/android-34/android.jar -d $B TaildroidUi.java
//   $A/build-tools/34.0.0/d8 --min-api 30 --lib $A/platforms/android-34/android.jar --output $B $B/*.class
//   (cd $B && zip -q t.jar classes.dex) && cp $B/t.jar ../taildroid-ui.jar
//
// It exits after IDLE_MS without a request: a connected UiAutomation can mute
// the phone's accessibility services, so it must not linger.
import android.app.UiAutomation;
import android.graphics.Point;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.nio.file.Files;

public final class TaildroidUi {
  static final long IDLE_MS = 60_000;
  static volatile long last = System.currentTimeMillis();

  public static void main(String[] args) throws Exception {
    Class<?> wrapper = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
    Object w = wrapper.getConstructor().newInstance();
    wrapper.getMethod("connect").invoke(w);
    wrapper.getMethod("setCompressedLayoutHierarchy", boolean.class).invoke(w, false);
    UiAutomation ua = (UiAutomation) wrapper.getMethod("getUiAutomation").invoke(w);

    Class<?> dmg = Class.forName("android.hardware.display.DisplayManagerGlobal");
    Object dm = dmg.getMethod("getInstance").invoke(null);
    Method realDisplay = dmg.getMethod("getRealDisplay", int.class);
    Method dump = Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper")
        .getMethod("dumpWindowToFile", AccessibilityNodeInfo.class, File.class, int.class, int.class, int.class);
    File file = new File("/data/local/tmp/taildroid-ui.xml");

    Thread idle = new Thread(() -> {
      while (System.currentTimeMillis() - last < IDLE_MS) {
        try { Thread.sleep(5_000); } catch (InterruptedException e) { return; }
      }
      System.exit(0);
    });
    idle.setDaemon(true);
    idle.start();

    PrintStream out = System.out;
    BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
    out.println("<<READY>>");
    out.flush();
    while (in.readLine() != null) {
      last = System.currentTimeMillis();
      try {
        // A short settle instead of uiautomator's full second of quiet; the
        // MCP server already pauses after a tap before it reads.
        try { ua.waitForIdle(100, 600); } catch (Exception busy) { }
        AccessibilityNodeInfo root = ua.getRootInActiveWindow();
        if (root == null) {
          out.println("ERROR: no active window");
        } else {
          Display d = (Display) realDisplay.invoke(dm, 0);
          Point size = new Point();
          d.getRealSize(size);
          dump.invoke(null, root, file, d.getRotation(), size.x, size.y);
          out.write(Files.readAllBytes(file.toPath()));
          out.println();
        }
      } catch (Throwable t) {
        out.println("ERROR: " + t);
      }
      out.println("<<END>>");
      out.flush();
    }
    System.exit(0);
  }
}
