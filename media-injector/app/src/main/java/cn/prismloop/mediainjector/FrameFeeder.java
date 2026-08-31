package cn.prismloop.mediainjector;

import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 帧序列播放器:按指定 fps 循环读取 Pod 内的裸帧数据并交给 PodProxy 注入。
 *
 * 目录格式(由 Python 侧 ffmpeg 预解码生成):
 *   /data/local/tmp/prismloop/fixtures/video-a/
 *     manifest.json  → {"width":640,"height":480,"format":"i420","fps":30,
 *                       "file":"video.bin","frames":150}
 *     video.bin      → 连续裸帧流(I420: w*h*3/2 字节/帧;RGBA: w*h*4 字节/帧)
 *
 * 兼容多文件模式(无 "file" 字段时):frame_0000.bin, frame_0001.bin, ...
 *
 * play() 切换目录只替换帧来源,不注销 CameraCallback、不重建设备节点会话——
 * 这就是架构文档 11 §2.2 要求的"无断流热切换"。
 */
public final class FrameFeeder {

    private static final String TAG = "FrameFeeder";

    public interface FeedListener {
        void onFeedError(String message);
    }

    private final PodProxy podProxy;
    private final HandlerThread feedThread;
    private final Handler feedHandler;

    /** 单文件模式:连续裸帧流。 */
    private volatile FileInputStream rawStream = null;
    private volatile int rawFrameBytes = 0;
    private volatile int rawTotalFrames = 0;

    /** 多文件模式(兜底)。 */
    private volatile File[] currentFrames = new File[0];

    private volatile int frameWidth = 0;
    private volatile int frameHeight = 0;
    private volatile int frameFormat = PodProxy.FORMAT_I420;
    private volatile long frameIntervalMs = 33;
    private volatile boolean running = false;
    private volatile int cursor = 0;
    private volatile long pushedCount = 0;
    private volatile String currentDir = "";
    private volatile String lastError = "";

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            try {
                byte[] data;
                if (rawStream != null) {
                    data = readRawFrame();
                } else if (currentFrames.length > 0) {
                    data = readFrame(currentFrames[cursor]);
                    cursor = (cursor + 1) % currentFrames.length;
                } else {
                    data = null;
                }
                if (data != null) {
                    podProxy.putVideoFrame(data);
                    pushedCount++;
                }
            } catch (Exception e) {
                lastError = e.getMessage() == null ? e.toString() : e.getMessage();
                Log.e(TAG, "feed tick failed: " + lastError);
            }
            feedHandler.postDelayed(this, frameIntervalMs);
        }
    };

    public FrameFeeder(PodProxy podProxy) {
        this.podProxy = podProxy;
        this.feedThread = new HandlerThread("frame-feeder");
        this.feedThread.start();
        this.feedHandler = new Handler(feedThread.getLooper());
    }

    /**
     * 开始播放(或切换到)指定帧序列目录。
     * 若已在播放:仅替换数据源与参数,feed 循环不中断(无断流热切换)。
     */
    public synchronized void play(String dir, int fallbackFps) throws IOException {
        File d = new File(dir);
        if (!d.isDirectory()) {
            throw new IOException("fixture dir not found: " + dir);
        }

        Manifest mf = readManifest(d);
        this.frameWidth = mf.width;
        this.frameHeight = mf.height;
        this.frameFormat = "rgba".equals(mf.format) ? PodProxy.FORMAT_RGBA_8888 : PodProxy.FORMAT_I420;
        long interval = mf.fps > 0 ? (1000 / mf.fps) : (1000 / fallbackFps);
        this.frameIntervalMs = Math.max(16, interval);

        // 关闭旧的原始流
        if (rawStream != null) {
            try {
                rawStream.close();
            } catch (IOException ignored) {
            }
            rawStream = null;
        }

        boolean singleFile = mf.file != null && !mf.file.isEmpty();
        if (singleFile) {
            File raw = new File(d, mf.file);
            if (!raw.isFile()) {
                throw new IOException("raw frame file not found: " + raw);
            }
            this.rawFrameBytes = frameBytes(mf);
            if (mf.frames > 0) {
                this.rawTotalFrames = mf.frames;
            } else {
                this.rawTotalFrames = (int) (raw.length() / rawFrameBytes);
            }
            if (rawFrameBytes <= 0 || raw.length() % rawFrameBytes != 0) {
                throw new IOException("raw file size " + raw.length()
                        + " not multiple of frame bytes " + rawFrameBytes);
            }
            this.rawStream = new FileInputStream(raw);
            this.currentFrames = new File[0];
        } else {
            List<File> frames = new ArrayList<>();
            for (int i = 0; ; i++) {
                File f = new File(d, String.format("frame_%04d.bin", i));
                if (!f.exists()) {
                    break;
                }
                frames.add(f);
            }
            if (frames.isEmpty()) {
                throw new IOException("no video.bin / frame_*.bin under " + dir);
            }
            this.currentFrames = frames.toArray(new File[0]);
        }

        this.currentDir = dir;
        boolean wasRunning = this.running;
        this.cursor = 0;
        this.running = true;

        // 通知 PodProxy 当前帧参数(宽高/格式可能与上次不同)
        try {
            podProxy.openCamera(frameWidth, frameHeight, frameFormat, 0, null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("camera open interrupted");
        }

        if (!wasRunning) {
            feedHandler.removeCallbacks(tick);
            feedHandler.post(tick);
        }
        Log.i(TAG, "play " + dir + " mode=" + (singleFile ? "raw" : "files")
                + " frames=" + rawTotalFrames + "/" + currentFrames.length
                + " " + frameWidth + "x" + frameHeight
                + " interval=" + frameIntervalMs + "ms hotSwitch=" + wasRunning);
    }

    public synchronized void stop() {
        running = false;
        feedHandler.removeCallbacks(tick);
    }

    public String getStatus() {
        return "{\"running\":" + running
                + ",\"dir\":\"" + currentDir + "\""
                + ",\"frames\":" + (rawStream != null ? rawTotalFrames : currentFrames.length)
                + ",\"size\":\"" + frameWidth + "x" + frameHeight + "\""
                + ",\"pushed\":" + pushedCount
                + ",\"lastError\":\"" + lastError.replace("\"", "'") + "\"}";
    }

    /** 单文件模式:顺序读一帧;读尽后 seek 回开头循环。 */
    private byte[] readRawFrame() throws IOException {
        FileInputStream in = rawStream;
        if (in == null) {
            return null;
        }
        byte[] buf = new byte[rawFrameBytes];
        int off = 0;
        while (off < buf.length) {
            int n = in.read(buf, off, buf.length - off);
            if (n < 0) {
                // 流结束 → 循环回开头
                in.getChannel().position(0);
                if (off == 0) {
                    return null;
                }
                int m = in.read(buf, off, buf.length - off);
                if (m < 0) {
                    return off == buf.length ? buf : null;
                }
                off += m;
                continue;
            }
            off += n;
        }
        return buf;
    }

    private int frameBytes(Manifest mf) {
        if ("rgba".equals(mf.format)) {
            return mf.width * mf.height * 4;
        }
        return mf.width * mf.height * 3 / 2; // I420
    }

    private byte[] readFrame(File f) throws IOException {
        byte[] buf = new byte[(int) f.length()];
        try (FileInputStream in = new FileInputStream(f)) {
            int off = 0;
            while (off < buf.length) {
                int n = in.read(buf, off, buf.length - off);
                if (n < 0) {
                    break;
                }
                off += n;
            }
        }
        return buf;
    }

    private Manifest readManifest(File dir) throws IOException {
        File mf = new File(dir, "manifest.json");
        Manifest m = new Manifest();
        if (mf.exists()) {
            String json = readFileAsString(mf);
            m.width = extractInt(json, "width", 640);
            m.height = extractInt(json, "height", 480);
            m.fps = extractInt(json, "fps", 30);
            m.format = extractString(json, "format", "i420");
            m.file = extractString(json, "file", null);
            m.frames = extractInt(json, "frames", 0);
        } else {
            m.width = 640;
            m.height = 480;
            m.fps = 30;
            m.format = "i420";
        }
        return m;
    }

    private static String readFileAsString(File f) throws IOException {
        byte[] buf = new byte[(int) f.length()];
        try (FileInputStream in = new FileInputStream(f)) {
            int off = 0;
            while (off < buf.length) {
                int n = in.read(buf, off, buf.length - off);
                if (n < 0) {
                    break;
                }
                off += n;
            }
        }
        return new String(buf, StandardCharsets.UTF_8);
    }

    private static int extractInt(String json, String key, int def) {
        java.util.regex.Matcher matcher = java.util.regex.Pattern
                .compile("\"" + key + "\"\\s*:\\s*(\\d+)")
                .matcher(json);
        return matcher.find() ? Integer.parseInt(matcher.group(1)) : def;
    }

    private static String extractString(String json, String key, String def) {
        java.util.regex.Matcher matcher = java.util.regex.Pattern
                .compile("\"" + key + "\"\\s*:\\s*\"([^\"]+)\"")
                .matcher(json);
        return matcher.find() ? matcher.group(1) : def;
    }

    private static final class Manifest {
        int width;
        int height;
        int fps;
        String format;
        String file;
        int frames;
    }
}
