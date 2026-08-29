package cn.prismloop.mediaprobe;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
        content.addView(preview, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));

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
        content.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
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
            cameraStatus.setText("Camera: opening id=" + selected);
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
        Surface surface = new Surface(preview.getSurfaceTexture());
        try {
            CaptureRequest.Builder request = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            request.addTarget(surface);
            request.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
            camera.createCaptureSession(Arrays.asList(surface), new CameraCaptureSession.StateCallback() {
                @Override public void onConfigured(CameraCaptureSession session) {
                    try {
                        cameraSession = session;
                        session.setRepeatingRequest(request.build(), null, mainHandler);
                        cameraStatus.setText("Camera: preview running; waiting for external frames");
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
