plugins {
    id("com.android.application")
}

android {
    namespace = "cn.prismloop.mediainjector"
    compileSdk = 34

    defaultConfig {
        applicationId = "cn.prismloop.mediainjector"
        minSdk = 27
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// Proxy SDK(proxysdk-0.2.2.3.2.aar)需手动下载放入 app/libs/
// 下载地址:https://docs.volcengine.com/docs/6394/1129851 「下载 Proxy SDK」章节
dependencies {
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.jar", "*.aar"))))
}
