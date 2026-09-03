package cn.prismloop.mediaprobe;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Matrix;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.util.Size;
import android.view.Gravity;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * A business-neutral observation point for cloud-phone camera and microphone injection.
 * It intentionally emits no pass/fail verdict and does not persist media content.
 */
public final class MainActivity extends Activity implements TextureView.SurfaceTextureListener {
    private static final String TAG = "MediaProbe";
    private static final int REQUEST_MEDIA_PERMISSIONS = 100;
    private static final int SAMPLE_RATE_HZ = 48_000;
    private static final int CHANNEL_COUNT = 1;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService audioExecutor = Executors.newSingleThreadExecutor();
    private TextureView preview;
    private TextView cameraStatus;
    private TextView audioStatus;
    private TextView permissionStatus;
    private CameraDevice camera;
    private CameraCaptureSession cameraSession;
    private AudioRecord audioRecord;
    private volatile boolean audioRunning;
    private long cameraFrameCount;
    private long audioSampleCount;
    private int sensorOrientationDegrees;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContent());
        requestMissingPermissions();
    }

    private View createContent() {
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(24, 24, 24, 24);

        TextView title = label("Prismloop Media I/O Probe", 22);
        content.addView(title);
        content.addView(label("Only observes Android Camera2 and AudioRecord. No business verdict is produced.", 14));

        preview = new TextureView(this);
        preview.setSurfaceTextureListener(this);

        ScrollView scroll = new ScrollView(this);
        LinearLayout diagnostics = new LinearLayout(this);
        diagnostics.setOrientation(LinearLayout.VERTICAL);
        permissionStatus = label("Permissions: checking", 14);
        cameraStatus = label("Camera: idle", 14);
        audioStatus = label("Microphone: idle", 14);
        diagnostics.addView(permissionStatus);
        diagnostics.addView(cameraStatus);
        diagnostics.addView(audioStatus);
        scroll.addView(diagnostics);

        boolean landscape = getResources().getConfiguration().orientation
                == android.content.res.Configuration.ORIENTATION_LANDSCAPE;
        if (landscape) {
            // 横屏:预览占左侧主区,诊断面板固定宽度靠右,避免文本挤压预览高度。
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.addView(preview, new LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.MATCH_PARENT, 1.0f));
            row.addView(scroll, new LinearLayout.LayoutParams(
                    (int) (150 * getResources().getDisplayMetrics().density),
                    LinearLayout.LayoutParams.MATCH_PARENT));
            content.addView(row, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));
        } else {
            content.addView(preview, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));
            content.addView(scroll, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        }
        return content;
    }

    private TextView label(String value, int sizeSp) {
        TextView label = new TextView(this);
        label.setText(value);
        label.setTextSize(sizeSp);
        label.setPadding(0, 10, 0, 10);
        label.setGravity(Gravity.START);
        return label;
    }

    private void requestMissingPermissions() {
        String[] required = new String[] {Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO};
        boolean missing = Arrays.stream(required)
                .anyMatch(permission -> checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED);
        if (missing) {
            requestPermissions(required, REQUEST_MEDIA_PERMISSIONS);
        } else {
            permissionStatus.setText("Permissions: camera + microphone granted");
            startAvailableInputs();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_MEDIA_PERMISSIONS) {
            boolean cameraGranted = checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
            boolean audioGranted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
            permissionStatus.setText("Permissions: camera=" + cameraGranted + ", microphone=" + audioGranted);
            startAvailableInputs();
        }
    }

    private void startAvailableInputs() {
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED && preview.isAvailable()) {
            openCamera();
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startMicrophone();
        }
    }

    private void openCamera() {
        try {
            CameraManager manager = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
            String selected = selectCamera(manager);
            if (selected == null) {
                cameraStatus.setText("Camera: no camera device reported");
                return;
            }
            CameraCharacteristics characteristics = manager.getCameraCharacteristics(selected);
            Integer orientation = characteristics.get(CameraCharacteristics.SENSOR_ORIENTATION);
            sensorOrientationDegrees = orientation == null ? 0 : orientation;
            Log.i(TAG, "sensorOrientation=" + sensorOrientationDegrees);
            manager.openCamera(selected, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice device) {
                    camera = device;
                    createPreviewSession();
                }

                @Override public void onDisconnected(CameraDevice device) {
                    cameraStatus.setText("Camera: disconnected");
                    device.close();
                }

                @Override public void onError(CameraDevice device, int error) {
                    cameraStatus.setText("Camera: error=" + error);
                    device.close();
                }
            }, mainHandler);
            cameraStatus.setText("Camera: opening id=" + selected
                    + "; sensorOrientation=" + sensorOrientationDegrees + "°");
        } catch (Exception error) {
            cameraStatus.setText("Camera: " + error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    private String selectCamera(CameraManager manager) throws Exception {
        String fallback = null;
        for (String id : manager.getCameraIdList()) {
            Integer facing = manager.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                return id;
            }
            fallback = id;
        }
        return fallback;
    }

    private void createPreviewSession() {
        if (camera == null || !preview.isAvailable()) return;
        SurfaceTexture texture = preview.getSurfaceTexture();
        Surface surface = new Surface(texture);
        try {
            // 流尺寸:优先取注入 profile(与 injector 下发的宽高一致),回退到 Surface 输出能力。
            Size streamSize = chooseStreamSize(camera.getId());
            if (streamSize != null) {
                texture.setDefaultBufferSize(streamSize.getWidth(), streamSize.getHeight());
            }
            applyPreviewRotation(streamSize);
            CaptureRequest.Builder request = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            request.addTarget(surface);
            request.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
            camera.createCaptureSession(Arrays.asList(surface), new CameraCaptureSession.StateCallback() {
                @Override public void onConfigured(CameraCaptureSession session) {
                    try {
                        cameraSession = session;
                        session.setRepeatingRequest(request.build(), null, mainHandler);
                        cameraStatus.setText("Camera: preview running; sensorOrientation="
                                + sensorOrientationDegrees + "°; waiting for external frames");
                    } catch (Exception error) {
                        cameraStatus.setText("Camera: preview request failed: " + error.getMessage());
                    }
                }

                @Override public void onConfigureFailed(CameraCaptureSession session) {
                    cameraStatus.setText("Camera: preview configuration failed");
                }
            }, mainHandler);
        } catch (Exception error) {
            cameraStatus.setText("Camera: session failed: " + error.getMessage());
        }
    }

    private Size chooseStreamSize(String cameraId) {
        try {
            CameraManager manager = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
            StreamConfigurationMap map = manager.getCameraCharacteristics(cameraId)
                    .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            if (map == null) return null;
            Size[] sizes = map.getOutputSizes(SurfaceTexture.class);
            if (sizes == null || sizes.length == 0) return null;
            // 按实际视图宽高比选择。sensorOrientation=90/270 时缓冲会被旋转后显示,
            // 因此应按"旋转后的宽高比"评估:swap 传感器的有效宽高比 = h/w。
            int viewWidth = preview.getWidth();
            int viewHeight = preview.getHeight();
            double targetAspect = (viewWidth > 0 && viewHeight > 0)
                    ? (double) viewWidth / viewHeight : 9.0 / 16.0;
            boolean swap = sensorOrientationDegrees == 90 || sensorOrientationDegrees == 270;
            Size best = null;
            double bestScore = Double.MAX_VALUE;
            for (Size candidate : sizes) {
                long pixels = (long) candidate.getWidth() * candidate.getHeight();
                if (pixels > 1280L * 720L) continue;
                double aspect = swap
                        ? candidate.getHeight() / (double) candidate.getWidth()
                        : candidate.getWidth() / (double) candidate.getHeight();
                double score = Math.abs(aspect - targetAspect) - pixels / 1e9;
                if (score < bestScore) {
                    bestScore = score;
                    best = candidate;
                }
            }
            return best;
        } catch (Exception error) {
            Log.w(TAG, "chooseStreamSize failed: " + error);
            return null;
        }
    }

    /**
     * 传感器方向补偿(竖屏 Activity,显示旋转 0°):
     * 显示时需将缓冲内容顺时针旋转 sensorOrientation 度(Camera2 规范),并等比 cover 填满视图。
     *
     * 关键语义:setTransform 的矩阵作用于"已被拉伸填满视图"的内容,矩阵处于视图坐标系。
     * 因此所有平移/缩放都必须以视图尺寸为基准;cover 等比(基于缓冲区坐标)需除以
     * 基础拉伸比(view/buffer)换算到视图坐标,得到非均匀的 sx/sy——对缓冲区内容而言
     * 仍是等比 cover。
     */
    private void applyPreviewRotation(Size streamSize) {
        int viewWidth = preview.getWidth();
        int viewHeight = preview.getHeight();
        Log.i(TAG, "applyPreviewRotation: sensorOrientation=" + sensorOrientationDegrees
                + ", stream=" + streamSize + ", view=" + viewWidth + "x" + viewHeight);
        if (streamSize == null || viewWidth == 0 || viewHeight == 0) {
            preview.setTransform(null);
            return;
        }
        float bufferWidth = streamSize.getWidth();
        float bufferHeight = streamSize.getHeight();
        boolean swap = sensorOrientationDegrees == 90 || sensorOrientationDegrees == 270;
        // 旋转后映射到视图的宽高。
        float mappedWidth = swap ? bufferHeight : bufferWidth;
        float mappedHeight = swap ? bufferWidth : bufferHeight;
        // cover 等比(缓冲区坐标系): 旋转后的缓冲铺满视图所需的最小等比。
        float coverScale = Math.max(viewWidth / mappedWidth, viewHeight / mappedHeight);
        // 换算到视图坐标(内容已被拉伸 view/buffer 倍):
        float sx = coverScale * bufferWidth / viewWidth;
        float sy = coverScale * bufferHeight / viewHeight;
        Matrix matrix = new Matrix();
        // 复合顺序(对点作用从右到左): 视图中心移到原点 → 非均匀缩放(抵消拉伸+cover)→
        // 顺时针旋转 sensorOrientation → 回到视图中心。
        matrix.setTranslate(viewWidth / 2f, viewHeight / 2f);
        matrix.preRotate(sensorOrientationDegrees);
        matrix.preScale(sx, sy);
        matrix.preTranslate(-viewWidth / 2f, -viewHeight / 2f);
        preview.setTransform(matrix);
    }

    private void startMicrophone() {
        if (audioRunning) return;
        int channelMask = AudioFormat.CHANNEL_IN_MONO;
        int encoding = AudioFormat.ENCODING_PCM_16BIT;
        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE_HZ, channelMask, encoding);
        if (minimum <= 0) {
            audioStatus.setText("Microphone: unavailable (min buffer=" + minimum + ")");
            return;
        }
        try {
            audioRecord = new AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE_HZ, channelMask, encoding, minimum * 2);
            if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
                audioStatus.setText("Microphone: AudioRecord not initialized");
                return;
            }
            audioRecord.startRecording();
            audioRunning = true;
            audioExecutor.execute(() -> readMicrophone(minimum));
        } catch (Exception error) {
            audioStatus.setText("Microphone: " + error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    private void readMicrophone(int bufferBytes) {
        short[] buffer = new short[bufferBytes / 2];
        while (audioRunning && audioRecord != null) {
            int read = audioRecord.read(buffer, 0, buffer.length, AudioRecord.READ_BLOCKING);
            if (read <= 0) continue;
            double meanSquare = 0;
            for (int i = 0; i < read; i++) {
                double normalized = buffer[i] / 32768.0;
                meanSquare += normalized * normalized;
            }
            double rms = Math.sqrt(meanSquare / read);
            double dbfs = rms <= 0.000001 ? -120.0 : 20.0 * Math.log10(rms);
            audioSampleCount += read;
            final String status = String.format(
                    "Microphone: PCM s16le, 48000 Hz, mono; samples=%d; RMS=%.5f (%.1f dBFS)",
                    audioSampleCount, rms, dbfs);
            mainHandler.post(() -> audioStatus.setText(status));
        }
    }

    @Override public void onSurfaceTextureAvailable(SurfaceTexture surface, int width, int height) { startAvailableInputs(); }
    @Override public void onSurfaceTextureSizeChanged(SurfaceTexture surface, int width, int height) { }
    @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture surface) { closeCamera(); return true; }

    @Override public void onSurfaceTextureUpdated(SurfaceTexture surface) {
        cameraFrameCount++;
        if (cameraFrameCount % 15 == 0) {
            cameraStatus.setText("Camera: preview frames=" + cameraFrameCount
                    + "; latest timestamp_ns=" + surface.getTimestamp());
        }
    }

    @Override protected void onStop() {
        super.onStop();
        stopMicrophone();
        closeCamera();
    }

    @Override protected void onDestroy() {
        audioExecutor.shutdownNow();
        super.onDestroy();
    }

    private void closeCamera() {
        if (cameraSession != null) { cameraSession.close(); cameraSession = null; }
        if (camera != null) { camera.close(); camera = null; }
    }

    private void stopMicrophone() {
        audioRunning = false;
        if (audioRecord != null) {
            try { audioRecord.stop(); } catch (IllegalStateException ignored) { }
            audioRecord.release();
            audioRecord = null;
        }
    }
}
