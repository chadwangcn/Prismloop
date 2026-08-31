package cn.prismloop.mediainjector;

import android.content.Context;
import android.util.Log;

import com.ss.device.audio.AudioSampleConfig;
import com.ss.device.audio.FileRecordType;
import com.ss.device.camera.VideoFrameConfig;
import com.ss.device.camera.VideoFrameFormat;
import com.ss.device.proxy.audio.AudioProxyManager;
import com.ss.device.proxy.camera.CameraProxyManager;
import com.ss.device.proxy.context.DeviceProxyContext;

/**
 * Proxy SDK(裸数据注入)唯一封装点。
 *
 * 官方文档:https://docs.volcengine.com/docs/6394/1129851
 * SDK:proxysdk-0.2.2.3.2.aar(实际包名 com.ss.device.*,经 javap 对真实 aar 校验)
 *
 * 本文件是整个工程唯一直接 import proxysdk 类的地方。
 *
 * 时序约束(官方文档明示):
 *  1. getCameraProxyManager() 之后必须 sleep 200ms 再 registerCallback;
 *  2. CameraCallback.onStart(id) 到达后才开始 putVideoFrame;
 *  3. AudioCallback.onStart() 到达后才开始 putAudioFrame;
 *  4. 视频仅 I420/NV21/RGBA 等(VideoFrameFormat),音频仅 PCM;
 *  5. 旗舰型实例,Pod 内运行,与客户端外部源/内部源注入互斥。
 */
public final class PodProxy {

    private static final String TAG = "PodProxy";

    /** 常量直接引用 SDK 枚举,避免硬编码值 */
    public static final int FORMAT_I420 = VideoFrameFormat.I420;
    public static final int FORMAT_RGBA_8888 = VideoFrameFormat.RGBA_8888;

    public interface CameraListener {
        void onCameraSessionOpened(int cameraId);
        void onCameraSessionClosed(int cameraId);
    }

    public interface AudioListener {
        void onAudioSessionOpened();
        void onAudioSessionClosed();
    }

    private final Context context;

    private DeviceProxyContext deviceProxyContext;
    private CameraProxyManager cameraProxyManager;
    private AudioProxyManager audioProxyManager;

    private CameraProxyManager.CameraCallback cameraCallback;
    private AudioProxyManager.AudioCallback audioCallback;

    private VideoFrameConfig currentVideoConfig;
    private volatile boolean cameraSessionOpen = false;
    private volatile boolean audioSessionOpen = false;

    private CameraListener cameraListener;
    private AudioListener audioListener;

    /** 最近一次 putVideoFrame 的返回值,用于 /status 诊断。 */
    public volatile boolean lastPutVideoFrameResult = false;
    public volatile int videoFrameCount = 0;
    public volatile int audioFrameCount = 0;

    public PodProxy(Context context) {
        this.context = context.getApplicationContext();
    }

    // ------------------------------------------------------------------
    // 初始化
    // ------------------------------------------------------------------

    /** 初始化 DeviceProxyContext。幂等。 */
    public synchronized void ensureInitialized() {
        if (deviceProxyContext != null) {
            return;
        }
        deviceProxyContext = DeviceProxyContext.getInstance(context);
        Log.i(TAG, "DeviceProxyContext acquired, sdk version="
                + DeviceProxyContext.getVersion());
    }

    // ------------------------------------------------------------------
    // 摄像头(视频注入)
    // ------------------------------------------------------------------

    /**
     * 注册摄像头回调。遵守官方 200ms 初始化间隔。
     *
     * @param width    帧宽(如 640)
     * @param height   帧高(如 480)
     * @param format   FORMAT_I420 或 FORMAT_RGBA_8888
     * @param cameraId 0=后置 1=前置
     */
    public synchronized void openCamera(int width, int height, int format, int cameraId,
                                        CameraListener listener) throws InterruptedException {
        ensureInitialized();

        this.cameraListener = listener;
        // VideoFrameConfig(width, height, format, rotation, cameraId)
        currentVideoConfig = new VideoFrameConfig(width, height, format, 0, cameraId);

        if (cameraProxyManager == null) {
            cameraProxyManager = deviceProxyContext.getCameraProxyManager();
            if (cameraProxyManager == null) {
                throw new IllegalStateException("getCameraProxyManager() returned null");
            }
            // 官方要求:getCameraProxyManager 后暂停 200ms 再注册回调
            Thread.sleep(200);
        }

        if (cameraCallback == null) {
            cameraCallback = new CameraProxyManager.CameraCallback() {
                @Override
                public void onStart(int id) {
                    cameraSessionOpen = true;
                    Log.i(TAG, "camera onStart id=" + id);
                    if (cameraListener != null) {
                        cameraListener.onCameraSessionOpened(id);
                    }
                }

                @Override
                public void onResume(int id) {
                    Log.i(TAG, "camera onResume id=" + id);
                }

                @Override
                public void onPause(int id) {
                    Log.i(TAG, "camera onPause id=" + id);
                }

                @Override
                public void onStop(int id) {
                    cameraSessionOpen = false;
                    Log.i(TAG, "camera onStop id=" + id);
                    if (cameraListener != null) {
                        cameraListener.onCameraSessionClosed(id);
                    }
                }

                @Override
                public void onRequest(int id) {
                    Log.i(TAG, "camera onRequest id=" + id);
                }
            };
            boolean ok = cameraProxyManager.registerCallback(cameraCallback);
            Log.i(TAG, "camera registerCallback = " + ok);
        }
    }

    /**
     * 推一帧视频。调用方必须已经通过 openCamera 完成注册;
     * 建议在 CameraListener.onCameraSessionOpened 之后调用。
     *
     * @param frame 帧数据(裸字节:I420 为 Y+U+V 平面;RGBA 为逐像素)
     */
    public void putVideoFrame(byte[] frame) {
        if (cameraProxyManager == null || currentVideoConfig == null) {
            lastPutVideoFrameResult = false;
            return;
        }
        lastPutVideoFrameResult = cameraProxyManager.putVideoFrame(
                frame, frame.length, currentVideoConfig);
        if (lastPutVideoFrameResult) {
            videoFrameCount++;
        }
    }

    public synchronized void closeCamera() {
        if (cameraProxyManager != null && cameraCallback != null) {
            cameraProxyManager.unregisterCallback(cameraCallback);
        }
        cameraSessionOpen = false;
    }

    // ------------------------------------------------------------------
    // 麦克风(音频注入)
    // ------------------------------------------------------------------

    /**
     * 文件循环模式(官方方式一):直接让 SDK 循环播放 PCM 文件。
     * 最稳的音频注入路径。
     *
     * 注意:必须先注册 AudioCallback 建立会话(AudioProxyService 报
     * "no audio call back on audio start!" 且 put audio frame failed),
     * 再 setAudioSampleConfigs + recordByFile。
     *
     * @param pcmPath Pod 内 PCM 文件绝对路径(16bit PCM)
     */
    public synchronized void audioFileLoop(String pcmPath, int sampleRate, int channels)
            throws InterruptedException {
        ensureInitialized();
        ensureAudioManager();

        // 1. 先注册回调建立会话(否则 put audio frame failed)
        ensureAudioCallbackRegistered(null);

        // 2. 配置采样参数 + 文件循环播放
        audioProxyManager.setAudioSampleConfigs(
                new AudioSampleConfig(sampleRate, channels, 2));
        audioProxyManager.recordByFile(pcmPath, FileRecordType.TYPE_CIRCLE);
        Log.i(TAG, "audio recordByFile started: " + pcmPath);
    }

    private void ensureAudioManager() throws InterruptedException {
        if (audioProxyManager == null) {
            audioProxyManager = deviceProxyContext.getAudioProxyManager();
            if (audioProxyManager == null) {
                throw new IllegalStateException("getAudioProxyManager() returned null");
            }
            Thread.sleep(200);
        }
    }

    private void ensureAudioCallbackRegistered(AudioListener listener) {
        if (audioCallback != null) {
            if (listener != null) {
                this.audioListener = listener;
            }
            return;
        }
        if (listener != null) {
            this.audioListener = listener;
        }
        audioCallback = new AudioProxyManager.AudioCallback() {
            @Override
            public void onStart() {
                audioSessionOpen = true;
                Log.i(TAG, "audio onStart");
                if (audioListener != null) {
                    audioListener.onAudioSessionOpened();
                }
            }

            @Override
            public void onResume() {
            }

            @Override
            public void onPause() {
            }

            @Override
            public void onStop() {
                audioSessionOpen = false;
                Log.i(TAG, "audio onStop");
                if (audioListener != null) {
                    audioListener.onAudioSessionClosed();
                }
            }

            @Override
            public void onRequest() {
            }
        };
        boolean ok = audioProxyManager.registerCallback(audioCallback);
        Log.i(TAG, "audio registerCallback = " + ok);
    }

    /**
     * 帧模式(官方方式二):注册回调后由调用方按 10ms 周期 putAudioFrame。
     */
    public synchronized void openAudioStream(int sampleRate, int channels, AudioListener listener)
            throws InterruptedException {
        ensureInitialized();
        ensureAudioManager();

        audioProxyManager.setAudioSampleConfigs(
                new AudioSampleConfig(sampleRate, channels, 2));
        audioProxyManager.setSamplePeriodInMs(10);
        ensureAudioCallbackRegistered(listener);
    }

    /** 推一块 PCM(帧模式)。单块采样点应为 sampleRate/100(10ms)。 */
    public void putAudioFrame(byte[] pcm) {
        if (audioProxyManager == null) {
            return;
        }
        audioProxyManager.putAudioFrame(pcm);
        audioFrameCount++;
    }

    public synchronized void closeAudio() {
        if (audioProxyManager != null && audioCallback != null) {
            audioProxyManager.unregisterCallback(audioCallback);
        }
        audioSessionOpen = false;
    }

    // ------------------------------------------------------------------
    // 状态
    // ------------------------------------------------------------------

    public boolean isCameraSessionOpen() {
        return cameraSessionOpen;
    }

    public boolean isAudioSessionOpen() {
        return audioSessionOpen;
    }

    public VideoFrameConfig getCurrentVideoConfig() {
        return currentVideoConfig;
    }
}
