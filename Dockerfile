# Prismloop Media I/O Harness 服务镜像(架构文档 12)
# 构建: docker build -t prismloop-media-harness:latest .
# 前提: 先本地构建两个 APK(见 media-injector/README.md 与 media-probe/)
#   gradle -p media-injector :app:assembleDebug
#   (media-probe APK 放到 media-probe/build-artifacts/)
# 运行: 见 config/media-harness.template.env(环境变量)+ Secret 注入 VOLC_AK/VOLC_SK

FROM python:3.12-slim

# ffmpeg: fixture 解码(I420/PCM);adb: Pod fixture 推送与 forward
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        adb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/prismloop

# 服务代码(纯标准库,无 pip 依赖)
COPY src/ src/

# APK 产物:injector(含 vendored proxysdk)+ media-probe
COPY media-injector/app/build/outputs/apk/debug/app-debug.apk apk/media-injector.apk
COPY media-probe/build-artifacts/prismloop-media-probe-debug.apk apk/media-probe.apk

# 数据卷:SQLite 状态库 + 证据输出
VOLUME ["/opt/prismloop/data", "/opt/prismloop/reports"]

EXPOSE 8787

# 健康检查:进程存活(详细探针见 GET /healthz)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=3)" || exit 1

CMD ["python3", "-m", "src.media_server", "--provider", "pod-injector"]
