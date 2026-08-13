package net.tunebox;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Self-update via GitHub Releases — the standard mechanism for a sideloaded app.
 *
 * A release is tagged with the versionCode as a plain integer (e.g. "v3") and
 * carries the built APK as an asset. On launch (throttled to once a day) and on
 * the in-app "检查更新" button, we ask the GitHub API for the latest release,
 * compare its version to ours, and if it's newer download the APK and fire the
 * system installer. This keeps BOTH the UI and the bundled yt-dlp fresh — the
 * latter is the real reason it matters, since yt-dlp breaks when YouTube changes.
 *
 * Set GITHUB_REPO once the repo exists. Until then the check is a no-op.
 */
public class Updater {

    static final String GITHUB_REPO = "HitokageCMD/music";

    private static final String PREFS = "tunebox_update";
    private static final long DAY_MS = 24 * 60 * 60 * 1000L;

    public static void autoCheck(Activity a) {
        if (!configured()) return;
        SharedPreferences sp = a.getSharedPreferences(PREFS, Activity.MODE_PRIVATE);
        long last = sp.getLong("last_check", 0);
        if (System.currentTimeMillis() - last < DAY_MS) return; // once a day is plenty
        sp.edit().putLong("last_check", System.currentTimeMillis()).apply();
        check(a, false);
    }

    /** Called from the web UI's 检查更新 button (userInitiated = true). */
    public static void check(Activity a, boolean userInitiated) {
        if (!configured()) {
            if (userInitiated) a.runOnUiThread(() ->
                    Toast.makeText(a, "自更新未配置（GITHUB_REPO）", Toast.LENGTH_SHORT).show());
            return;
        }
        new Thread(() -> {
            try {
                JSONObject rel = fetchLatestRelease();
                int remote = parseVersion(rel.optString("tag_name"));
                int local = currentVersionCode(a);
                String apkUrl = findApkAsset(rel);
                if (remote > local && apkUrl != null) {
                    String name = rel.optString("name", rel.optString("tag_name"));
                    a.runOnUiThread(() -> promptInstall(a, name, apkUrl));
                } else if (userInitiated) {
                    a.runOnUiThread(() ->
                            Toast.makeText(a, "已是最新版本", Toast.LENGTH_SHORT).show());
                }
            } catch (Exception e) {
                if (userInitiated) a.runOnUiThread(() ->
                        Toast.makeText(a, "检查更新失败: " + e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "tunebox-update").start();
    }

    // --- internals ---------------------------------------------------------

    private static boolean configured() {
        return GITHUB_REPO != null && !GITHUB_REPO.contains("OWNER") && GITHUB_REPO.contains("/");
    }

    private static int currentVersionCode(Activity a) {
        try {
            return (int) a.getPackageManager()
                    .getPackageInfo(a.getPackageName(), 0).getLongVersionCode();
        } catch (PackageManager.NameNotFoundException e) {
            return 0;
        }
    }

    /** "v3" / "3" / "v0.3" -> 3. First run of digits wins. */
    private static int parseVersion(String tag) {
        String digits = tag == null ? "" : tag.replaceAll("[^0-9].*$", "").replaceAll("[^0-9]", "");
        if (digits.isEmpty()) {
            // fall back to the last integer in the tag
            String[] parts = (tag == null ? "" : tag).split("[^0-9]+");
            for (int i = parts.length - 1; i >= 0; i--) if (!parts[i].isEmpty()) return Integer.parseInt(parts[i]);
            return 0;
        }
        return Integer.parseInt(digits);
    }

    private static JSONObject fetchLatestRelease() throws Exception {
        String url = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest";
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestProperty("Accept", "application/vnd.github+json");
        c.setRequestProperty("User-Agent", "tunebox");
        c.setConnectTimeout(15000);
        c.setReadTimeout(15000);
        try (InputStream in = c.getInputStream()) {
            return new JSONObject(readAll(in));
        } finally {
            c.disconnect();
        }
    }

    private static String findApkAsset(JSONObject rel) {
        JSONArray assets = rel.optJSONArray("assets");
        if (assets == null) return null;
        for (int i = 0; i < assets.length(); i++) {
            JSONObject asset = assets.optJSONObject(i);
            String n = asset == null ? "" : asset.optString("name", "");
            if (n.toLowerCase().endsWith(".apk")) return asset.optString("browser_download_url");
        }
        return null;
    }

    private static void promptInstall(Activity a, String name, String apkUrl) {
        new AlertDialog.Builder(a)
                .setTitle("有新版本")
                .setMessage((name == null || name.isEmpty() ? "发现更新" : name) + "\n\n现在下载并安装？")
                .setPositiveButton("更新", (d, w) -> download(a, apkUrl))
                .setNegativeButton("以后", null)
                .show();
    }

    private static void download(Activity a, String apkUrl) {
        Toast.makeText(a, "开始下载更新…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                File apk = new File(a.getCacheDir(), "update.apk");
                HttpURLConnection c = (HttpURLConnection) new URL(apkUrl).openConnection();
                c.setRequestProperty("User-Agent", "tunebox");
                c.setInstanceFollowRedirects(true);
                c.setConnectTimeout(20000);
                try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(apk)) {
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                }
                c.disconnect();
                a.runOnUiThread(() -> install(a, apk));
            } catch (Exception e) {
                a.runOnUiThread(() ->
                        Toast.makeText(a, "下载失败: " + e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "tunebox-download").start();
    }

    private static void install(Activity a, File apk) {
        Uri uri = FileProvider.getUriForFile(a, a.getPackageName() + ".fileprovider", apk);
        Intent i = new Intent(Intent.ACTION_VIEW);
        i.setDataAndType(uri, "application/vnd.android.package-archive");
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        a.startActivity(i);
    }

    private static String readAll(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        return bos.toString("UTF-8");
    }
}
