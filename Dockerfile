FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV GRADLE_HOME=/opt/gradle
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/jvm/java-17-openjdk-amd64/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/opt/gradle/bin"

# Install Java and required tools.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        ca-certificates \
        wget \
        unzip \
        git; \
    java -version; \
    rm -rf /var/lib/apt/lists/*

# Install Gradle 8.9.
RUN set -eux; \
    wget --https-only --tries=3 --timeout=30 \
        https://services.gradle.org/distributions/gradle-8.9-bin.zip \
        -O /tmp/gradle.zip; \
    unzip -q /tmp/gradle.zip -d /opt; \
    mv /opt/gradle-8.9 "${GRADLE_HOME}"; \
    rm -f /tmp/gradle.zip; \
    gradle --version

# Install Android command-line tools.
RUN set -eux; \
    mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools"; \
    wget --https-only --tries=3 --timeout=30 \
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
        -O /tmp/android-tools.zip; \
    unzip -q /tmp/android-tools.zip -d /tmp/android-tools; \
    mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools/latest"; \
    cp -a /tmp/android-tools/cmdline-tools/. \
        "${ANDROID_SDK_ROOT}/cmdline-tools/latest/"; \
    rm -rf /tmp/android-tools /tmp/android-tools.zip; \
    test -x "${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager"; \
    sdkmanager --version

# Accept licenses and install Android SDK packages.
RUN set -eux; \
    yes | sdkmanager --sdk_root="${ANDROID_SDK_ROOT}" --licenses \
        || test "$?" -eq 141; \
    sdkmanager --sdk_root="${ANDROID_SDK_ROOT}" \
        "platform-tools" \
        "platforms;android-35" \
        "build-tools;35.0.0"; \
    test -d "${ANDROID_SDK_ROOT}/platforms/android-35"; \
    test -d "${ANDROID_SDK_ROOT}/build-tools/35.0.0"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py

# Verify all build tools.
RUN set -eux; \
    command -v java; \
    command -v gradle; \
    command -v sdkmanager; \
    java -version; \
    gradle --version; \
    sdkmanager --sdk_root="${ANDROID_SDK_ROOT}" --list_installed

EXPOSE 10000

CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000} main:app"]
