plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.ophelia.extractor"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.ophelia.extractor"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            // Sideloaded personal tool: sign release with the debug key so the
            // APK installs as-is. Swap in a real keystore to publish it.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        // Listing every installed app is the whole point of this app.
        disable += "QueryAllPackagesPermission"
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
