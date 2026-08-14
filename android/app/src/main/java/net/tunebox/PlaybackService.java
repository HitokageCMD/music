package net.tunebox;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Build;
import android.os.IBinder;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;

import androidx.core.app.NotificationCompat;
import androidx.media.session.MediaButtonReceiver;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Foreground media service: a real MediaSession + MediaStyle notification so the
 * lock screen and notification shade get play/pause/next/prev. The WebView's
 * &lt;audio&gt; is the actual player — JS pushes state here via update(), and the
 * session's button callbacks run JS back on the WebView (set as the Controller).
 * This is what the WebView's own MediaSession failed to surface.
 */
public class PlaybackService extends Service {

    public interface Controller {
        void play();
        void pause();
        void next();
        void prev();
        void seek(long ms);
        void like();  // 收藏/取消收藏当前歌(锁屏/通知上的喜欢按钮)
    }

    private static final String CHANNEL = "tunebox_media";
    private static final int NOTIF_ID = 1;
    static final String ACTION_LIKE = "net.tunebox.action.LIKE"; // 通知喜欢按钮触发

    static PlaybackService instance;
    static Controller controller;

    private MediaSessionCompat session;
    private String title = "tunebox", artist = "";
    private boolean playing = false;
    private boolean liked = false;
    private long positionMs = 0, durationMs = 0;
    private Bitmap art;
    private String artUrlLoaded = "";

    static void setController(Controller c) { controller = c; }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;

        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "tunebox", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }

        session = new MediaSessionCompat(this, "tunebox");
        // Without these, the system's transport controls (lock screen / shade)
        // are drawn but their presses never reach the callback — exactly the
        // "buttons do nothing" symptom.
        session.setFlags(MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setSessionActivity(PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        session.setCallback(new MediaSessionCompat.Callback() {
            @Override public void onPlay() { if (controller != null) controller.play(); }
            @Override public void onPause() { if (controller != null) controller.pause(); }
            @Override public void onSkipToNext() { if (controller != null) controller.next(); }
            @Override public void onSkipToPrevious() { if (controller != null) controller.prev(); }
            @Override public void onSeekTo(long pos) { if (controller != null) controller.seek(pos); }
            @Override public void onStop() { if (controller != null) controller.pause(); }
        });
        session.setActive(true);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_LIKE.equals(intent.getAction())) {
            if (controller != null) controller.like();
        } else {
            // Route media-button intents (from the notification actions) to the callback.
            MediaButtonReceiver.handleIntent(session, intent);
        }
        startForeground(NOTIF_ID, buildNotification());
        return START_STICKY;
    }

    /** Called from the JS bridge whenever playback state changes. */
    public void update(String title, String artist, String artUrl,
                       boolean playing, long positionMs, long durationMs, boolean liked) {
        this.title = title == null || title.isEmpty() ? "tunebox" : title;
        this.artist = artist == null ? "" : artist;
        this.playing = playing;
        this.liked = liked;
        this.positionMs = positionMs;
        this.durationMs = durationMs;

        if (artUrl != null && !artUrl.equals(artUrlLoaded)) {
            artUrlLoaded = artUrl;
            loadArtAsync(artUrl);
        }
        applyMetadata();
        applyState();
        getSystemService(NotificationManager.class).notify(NOTIF_ID, buildNotification());
    }

    private void applyMetadata() {
        MediaMetadataCompat.Builder b = new MediaMetadataCompat.Builder()
                .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
                .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
                .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs);
        if (art != null) {
            b.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, art);
            b.putBitmap(MediaMetadataCompat.METADATA_KEY_ART, art);
        }
        session.setMetadata(b.build());
    }

    private void applyState() {
        long actions = PlaybackStateCompat.ACTION_PLAY | PlaybackStateCompat.ACTION_PAUSE
                | PlaybackStateCompat.ACTION_PLAY_PAUSE
                | PlaybackStateCompat.ACTION_SKIP_TO_NEXT
                | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
                | PlaybackStateCompat.ACTION_SEEK_TO | PlaybackStateCompat.ACTION_STOP;
        session.setPlaybackState(new PlaybackStateCompat.Builder()
                .setActions(actions)
                .setState(playing ? PlaybackStateCompat.STATE_PLAYING : PlaybackStateCompat.STATE_PAUSED,
                        positionMs, 1f)
                .build());
    }

    private Notification buildNotification() {
        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder n = new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(artist)
                .setLargeIcon(art)
                .setContentIntent(open)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setOngoing(playing)
                .setOnlyAlertOnce(true)
                .addAction(new NotificationCompat.Action(
                        android.R.drawable.ic_media_previous, "上一首",
                        MediaButtonReceiver.buildMediaButtonPendingIntent(
                                this, PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS)))
                .addAction(playing
                        ? new NotificationCompat.Action(android.R.drawable.ic_media_pause, "暂停",
                            MediaButtonReceiver.buildMediaButtonPendingIntent(this, PlaybackStateCompat.ACTION_PLAY_PAUSE))
                        : new NotificationCompat.Action(android.R.drawable.ic_media_play, "播放",
                            MediaButtonReceiver.buildMediaButtonPendingIntent(this, PlaybackStateCompat.ACTION_PLAY_PAUSE)))
                .addAction(new NotificationCompat.Action(
                        android.R.drawable.ic_media_next, "下一首",
                        MediaButtonReceiver.buildMediaButtonPendingIntent(
                                this, PlaybackStateCompat.ACTION_SKIP_TO_NEXT)))
                .addAction(new NotificationCompat.Action(
                        liked ? R.drawable.ic_heart_filled : R.drawable.ic_heart_outline,
                        liked ? "已喜欢" : "喜欢",
                        PendingIntent.getService(this, 1,
                                new Intent(this, PlaybackService.class).setAction(ACTION_LIKE),
                                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE)))
                .setStyle(new androidx.media.app.NotificationCompat.MediaStyle()
                        .setMediaSession(session.getSessionToken())
                        .setShowActionsInCompactView(0, 1, 2));
        return n.build();
    }

    private void loadArtAsync(String url) {
        new Thread(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(8000);
                try (InputStream in = c.getInputStream()) {
                    Bitmap bmp = BitmapFactory.decodeStream(in);
                    if (bmp != null) {
                        art = bmp;
                        applyMetadata();
                        getSystemService(NotificationManager.class).notify(NOTIF_ID, buildNotification());
                    }
                }
                c.disconnect();
            } catch (Exception ignored) {}
        }, "tunebox-art").start();
    }

    @Override
    public void onDestroy() {
        if (session != null) { session.setActive(false); session.release(); }
        if (instance == this) instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
