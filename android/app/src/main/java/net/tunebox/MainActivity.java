package net.tunebox;

import android.Manifest;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.ComponentActivity;
import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.app.NotificationManagerCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;

public class MainActivity extends ComponentActivity {

    private static final int PORT = 8730;
    private static final String URL = "http://127.0.0.1:" + PORT;
    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        if (Build.VERSION.SDK_INT >= 33
                && ActivityCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this, new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }

        web = new WebView(this);
        setContentView(web);
        configureWebView(web);

        // Media session button presses (lock screen / notification) run JS on the
        // WebView, which is where the actual <audio> player lives.
        PlaybackService.setController(new PlaybackService.Controller() {
            public void play()  { runJs("var a=document.getElementById('audio'); if(a) a.play();"); }
            public void pause() { runJs("var a=document.getElementById('audio'); if(a) a.pause();"); }
            public void next()  { runJs("window.__tuneboxNext && window.__tuneboxNext();"); }
            public void prev()  { runJs("window.__tuneboxPrev && window.__tuneboxPrev();"); }
            public void seek(long ms) { runJs("window.__tuneboxSeek && window.__tuneboxSeek(" + ms + ");"); }
        });

        // Keeps the process alive when backgrounded, and now carries the media
        // notification with play/pause/next/prev.
        Intent svc = new Intent(this, PlaybackService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);

        startBackend();
    }

    private void startSafe(Intent i) {
        try { startActivity(i); } catch (Exception e) { openAppDetails(); }
    }

    private void openAppDetails() {
        try {
            Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception ignored) {}
    }

    private void runJs(String js) {
        runOnUiThread(() -> { if (web != null) web.evaluateJavascript(js, null); });

        // Once a day, quietly check GitHub Releases for a newer APK (keeps yt-dlp fresh).
        Updater.autoCheck(this);
    }

    /** Bridge exposed to the web UI as window.tunebox. Absent in the PWA. */
    private class WebBridge {
        @JavascriptInterface
        public void checkUpdate() {
            Updater.check(MainActivity.this, true);
        }
        @JavascriptInterface
        public boolean isApp() {
            return true;
        }
        /** {"notif":bool,"battery":bool} — which background permissions are granted. */
        @JavascriptInterface
        public String permStatus() {
            boolean notif = Build.VERSION.SDK_INT >= 33
                    ? ActivityCompat.checkSelfPermission(MainActivity.this, Manifest.permission.POST_NOTIFICATIONS)
                        == PackageManager.PERMISSION_GRANTED
                    : NotificationManagerCompat.from(MainActivity.this).areNotificationsEnabled();
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            boolean battery = pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
            return "{\"notif\":" + notif + ",\"battery\":" + battery + "}";
        }

        @JavascriptInterface
        public void requestNotif() {
            runOnUiThread(() -> {
                if (Build.VERSION.SDK_INT >= 33 && ActivityCompat.checkSelfPermission(
                        MainActivity.this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(MainActivity.this,
                            new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2);
                } else {
                    Intent i = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                    startSafe(i);
                }
            });
        }

        @JavascriptInterface
        public void requestBattery() {
            runOnUiThread(() -> startSafe(new Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName()))));
        }

        @JavascriptInterface
        public void openAutostart() {
            runOnUiThread(() -> {
                // EMUI/other auto-start managers vary; try known components, then fall back.
                String[][] comps = {
                    {"com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                    {"com.huawei.systemmanager", "com.huawei.systemmanager.appcontrol.activity.StartupAppControlActivity"},
                    {"com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"},
                };
                for (String[] c : comps) {
                    Intent i = new Intent();
                    i.setComponent(new ComponentName(c[0], c[1]));
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    try { startActivity(i); return; } catch (Exception ignored) {}
                }
                openAppDetails();
            });
        }

        /** JS pushes playback state here; drives the media session + notification. */
        @JavascriptInterface
        public void updateMedia(String json) {
            try {
                JSONObject o = new JSONObject(json);
                PlaybackService s = PlaybackService.instance;
                if (s != null) {
                    s.update(o.optString("title"), o.optString("artist"), o.optString("art"),
                            o.optBoolean("playing"),
                            (long) o.optDouble("position", 0), (long) o.optDouble("duration", 0));
                }
            } catch (Exception ignored) {}
        }
    }

    private void configureWebView(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);       // localStorage AND IndexedDB (offline downloads)
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);  // let audio start on its own
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        w.addJavascriptInterface(new WebBridge(), "tunebox"); // window.tunebox.checkUpdate()
        w.setWebChromeClient(new WebChromeClient());
        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, int code, String desc, String url) {
                // Server may not be listening for the first instant — retry.
                new Handler(Looper.getMainLooper()).postDelayed(() -> v.loadUrl(URL), 500);
            }
        });
    }

    private void startBackend() {
        new Thread(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                File web = new File(getFilesDir(), "web");
                copyAssetDir("web", web);

                Python py = Python.getInstance();
                PyObject boot = py.getModule("tunebox_boot");
                boot.callAttr("start",
                        new File(getFilesDir(), "data").getAbsolutePath(),
                        web.getAbsolutePath(),
                        PORT);

                waitForPort(PORT, 15000);
                runOnUiThread(() -> this.web.loadUrl(URL));
            } catch (Throwable t) {
                runOnUiThread(() -> Toast.makeText(
                        this, "启动失败: " + t.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "tunebox-boot").start();
    }

    private void waitForPort(int port, long timeoutMs) {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try (Socket s = new Socket("127.0.0.1", port)) {
                return; // connected — server is up
            } catch (Exception e) {
                try { Thread.sleep(150); } catch (InterruptedException ignored) {}
            }
        }
    }

    /** Recursively copy an APK asset directory to the filesystem, overwriting. */
    private void copyAssetDir(String assetPath, File dest) throws Exception {
        String[] entries = getAssets().list(assetPath);
        if (entries == null || entries.length == 0) {
            copyAssetFile(assetPath, dest);
            return;
        }
        if (!dest.exists()) dest.mkdirs();
        for (String name : entries) {
            copyAssetDir(assetPath + "/" + name, new File(dest, name));
        }
    }

    private void copyAssetFile(String assetPath, File dest) throws Exception {
        File parent = dest.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        try (InputStream in = getAssets().open(assetPath);
             OutputStream out = new FileOutputStream(dest)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        }
    }

    @Override
    public void onRequestPermissionsResult(int req, @NonNull String[] perms, @NonNull int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        stopService(new Intent(this, PlaybackService.class));
        super.onDestroy();
    }
}
