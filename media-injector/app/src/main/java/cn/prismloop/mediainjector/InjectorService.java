package cn.prismloop.mediainjector;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.util.Log;

/**
 * 常驻前台服务:持有 PodProxy(Proxy SDK 会话)+ FrameFeeder(帧序列播放)
 * + HttpApi(127.0.0.1:18080 控制通道)。
 *
 * 由 MainActivity 启动;Python harness 通过 adb forward 控制注入。
 */
public final class InjectorService extends Service implements HttpApi.Controller {

    private static final String TAG = "InjectorService";
    public static final int PORT = 18080;
    private static final String CHANNEL_ID = "prismloop-injector";

    private PodProxy podProxy;
    private FrameFeeder frameFeeder;
    private HttpApi httpApi;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        startForegroundWithNotification();

        podProxy = new PodProxy(this);
        frameFeeder = new FrameFeeder(podProxy);
        httpApi = new HttpApi(PORT, this);

        try {
            httpApi.start();
        } catch (Exception e) {
            Log.e(TAG, "http api start failed: " + e);
        }

        MainActivity.InjectorServiceHolder.instance = this;
        Log.i(TAG, "InjectorService ready on port " + PORT);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (httpApi != null) {
            httpApi.stop();
        }
        if (frameFeeder != null) {
            frameFeeder.stop();
        }
        if (podProxy != null) {
            podProxy.closeCamera();
            podProxy.closeAudio();
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    // HttpApi.Controller 实现
    // ------------------------------------------------------------------

    @Override
    public String status() {
        return "{\"service\":\"prismloop-media-injector\""
                + ",\"port\":" + PORT
                + ",\"camera\":" + podProxy.isCameraSessionOpen()
                + ",\"audio\":" + podProxy.isAudioSessionOpen()
                + ",\"video_frames_pushed\":" + podProxy.videoFrameCount
                + ",\"audio_frames_pushed\":" + podProxy.audioFrameCount
                + ",\"feeder\":" + frameFeeder.getStatus()
                + "}";
    }

    @Override
    public String cameraSequence(String dir, int fps) throws Exception {
        frameFeeder.play(dir, fps);
        return "{\"ok\":true,\"action\":\"camera/sequence\"}";
    }

    @Override
    public String cameraStop() {
        frameFeeder.stop();
        return "{\"ok\":true,\"action\":\"camera/stop\"}";
    }

    @Override
    public String cameraFrame(byte[] data) throws Exception {
        podProxy.putVideoFrame(data);
        return "{\"ok\":true,\"pushed\":" + podProxy.lastPutVideoFrameResult + "}";
    }

    @Override
    public String audioFile(String path, int sampleRate, int channels) throws Exception {
        podProxy.audioFileLoop(path, sampleRate, channels);
        return "{\"ok\":true,\"action\":\"audio/file\"}";
    }

    @Override
    public String audioStop() {
        podProxy.closeAudio();
        return "{\"ok\":true,\"action\":\"audio/stop\"}";
    }

    // ------------------------------------------------------------------
    // 前台通知
    // ------------------------------------------------------------------

    private void startForegroundWithNotification() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Prismloop Injector", NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(channel);
        }
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_sys_headset)
                .setContentTitle("Prismloop Media Injector")
                .setContentText("injecting on 127.0.0.1:" + PORT)
                .setOngoing(true)
                .build();
        startForeground(1, notification);
    }
}
