package cn.prismloop.mediainjector;

import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * localhost HTTP 控制通道(默认 127.0.0.1:18080)。
 *
 * Python 侧通过 `adb forward tcp:18080 tcp:18080` 直连本服务,驱动注入。
 * 仅接受 loopback 连接,不对外暴露。
 *
 * 路由:
 *   GET  /status                → 整体状态 JSON
 *   POST /camera/sequence  body JSON: {"dir":"...","fps":30}
 *        开始或无断流切换帧序列播放
 *   POST /camera/stop            → 停止帧序列(不注销设备回调)
 *   POST /camera/frame    body: 二进制帧数据(单帧直推)
 *   POST /audio/file       body JSON: {"path":"...","sampleRate":48000,"channels":2}
 *        PCM 文件循环注入(官方 recordByFile)
 *   POST /audio/stop             → 停止音频
 *
 * 实现:请求行/头部用字节级手工解析(避免 Reader 预读吞掉 body);
 * body 按 Content-Length 精确字节读取,二进制安全。
 */
public final class HttpApi {

    private static final String TAG = "HttpApi";

    public interface Controller {
        String status();
        String cameraSequence(String dir, int fps) throws Exception;
        String cameraStop();
        String cameraFrame(byte[] data) throws Exception;
        String audioFile(String path, int sampleRate, int channels) throws Exception;
        String audioStop();
    }

    private final int port;
    private final Controller controller;
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private ServerSocket serverSocket;
    private volatile boolean running = false;

    public HttpApi(int port, Controller controller) {
        this.port = port;
        this.controller = controller;
    }

    public void start() throws IOException {
        serverSocket = new ServerSocket(port, 8, null); // null → 仅 loopback 绑定
        running = true;
        Thread acceptor = new Thread(new Runnable() {
            @Override
            public void run() {
                while (running) {
                    try {
                        Socket client = serverSocket.accept();
                        pool.execute(() -> handle(client));
                    } catch (IOException e) {
                        if (running) {
                            Log.e(TAG, "accept failed: " + e);
                        }
                    }
                }
            }
        }, "http-api-acceptor");
        acceptor.setDaemon(true);
        acceptor.start();
        Log.i(TAG, "HTTP API listening on 127.0.0.1:" + port);
    }

    public void stop() {
        running = false;
        try {
            if (serverSocket != null) {
                serverSocket.close();
            }
        } catch (IOException ignored) {
        }
        pool.shutdownNow();
    }

    // ------------------------------------------------------------------
    // 请求处理(字节级解析)
    // ------------------------------------------------------------------

    private void handle(Socket socket) {
        try (Socket s = socket) {
            s.setSoTimeout(20000);
            InputStream in = s.getInputStream();
            OutputStream out = s.getOutputStream();

            String requestLine = readLine(in);
            if (requestLine == null || requestLine.isEmpty()) {
                return;
            }
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) {
                respond(out, 400, "{\"error\":\"bad request line\"}");
                return;
            }
            String method = parts[0];
            String path = parts[1];

            // 头部:逐字节读行,只记录 Content-Length
            int contentLength = 0;
            String line;
            while ((line = readLine(in)) != null && !line.isEmpty()) {
                int colon = line.indexOf(':');
                if (colon > 0) {
                    String key = line.substring(0, colon).trim().toLowerCase();
                    String value = line.substring(colon + 1).trim();
                    if ("content-length".equals(key)) {
                        contentLength = Integer.parseInt(value);
                    }
                }
            }

            // body:按 Content-Length 精确字节读取(二进制安全)
            byte[] body = new byte[0];
            if (contentLength > 0) {
                body = readFully(in, contentLength);
            }

            String response = route(method, path, body);
            respond(out, 200, response);
        } catch (Exception e) {
            Log.e(TAG, "handle error: " + e);
        }
    }

    /** 从流中读一行(HTTP 头,CRLF/LF 均兼容)。流结束返回 null。 */
    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(128);
        int prev = -1;
        while (true) {
            int b = in.read();
            if (b < 0) {
                return buf.size() == 0 ? null : buf.toString("ISO-8859-1");
            }
            if (b == '\n') {
                if (prev == '\r' && buf.size() > 0) {
                    // 去掉末尾 \r
                    byte[] raw = buf.toByteArray();
                    return new String(raw, 0, raw.length - 1, StandardCharsets.ISO_8859_1);
                }
                return buf.toString("ISO-8859-1");
            }
            buf.write(b);
            prev = b;
        }
    }

    /** 精确读取 n 字节。 */
    private static byte[] readFully(InputStream in, int n) throws IOException {
        byte[] buf = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(buf, off, n - off);
            if (r < 0) {
                throw new IOException("unexpected EOF at " + off + "/" + n);
            }
            off += r;
        }
        return buf;
    }

    private String route(String method, String path, byte[] body) {
        try {
            if ("GET".equals(method) && "/status".equals(path)) {
                return controller.status();
            }
            if ("POST".equals(method)) {
                Map<String, String> json = parseSimpleJson(
                        new String(body, StandardCharsets.UTF_8));
                switch (path) {
                    case "/camera/sequence":
                        String dir = json.getOrDefault("dir", "");
                        int fps = Integer.parseInt(json.getOrDefault("fps", "30"));
                        requireNonEmpty(dir, "dir");
                        return controller.cameraSequence(dir, fps);
                    case "/camera/stop":
                        return controller.cameraStop();
                    case "/camera/frame":
                        requireNonEmpty(body, "frame body");
                        return controller.cameraFrame(body);
                    case "/audio/file":
                        String pcmPath = json.getOrDefault("path", "");
                        int sampleRate = Integer.parseInt(
                                json.getOrDefault("sampleRate", "48000"));
                        int channels = Integer.parseInt(
                                json.getOrDefault("channels", "2"));
                        requireNonEmpty(pcmPath, "path");
                        return controller.audioFile(pcmPath, sampleRate, channels);
                    case "/audio/stop":
                        return controller.audioStop();
                    default:
                        return "{\"error\":\"unknown path " + path + "\"}";
                }
            }
            return "{\"error\":\"unsupported method\"}";
        } catch (IllegalArgumentException e) {
            return "{\"error\":\"" + e.getMessage() + "\"}";
        } catch (Exception e) {
            String msg = e.getMessage() == null ? e.toString() : e.getMessage();
            return "{\"error\":\"" + msg.replace("\"", "'") + "\"}";
        }
    }

    private void respond(OutputStream out, int code, String json) throws IOException {
        byte[] payload = json.getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 " + code + " OK\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: " + payload.length + "\r\n"
                + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(payload);
        out.flush();
    }

    private void requireNonEmpty(String value, String name) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("missing field: " + name);
        }
    }

    private void requireNonEmpty(byte[] value, String name) {
        if (value == null || value.length == 0) {
            throw new IllegalArgumentException("missing body: " + name);
        }
    }

    /** 极简 JSON 提取:{"key":"value","key2":123} → map,满足本 API 的扁平结构。 */
    static Map<String, String> parseSimpleJson(String json) {
        Map<String, String> out = new HashMap<>();
        if (json == null || json.isEmpty()) {
            return out;
        }
        java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("\"([A-Za-z0-9_]+)\"\\s*:\\s*(\"[^\"]*\"|-?\\d+(?:\\.\\d+)?)")
                .matcher(json);
        while (m.find()) {
            String key = m.group(1);
            String raw = m.group(2);
            if (raw.startsWith("\"")) {
                out.put(key, raw.substring(1, raw.length() - 1));
            } else {
                out.put(key, raw);
            }
        }
        return out;
    }
}
