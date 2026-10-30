# App Extractor (Android)

A small standalone Android app that saves the APK files of apps already
installed on your phone, so you (or Ophelia) can study how they work. No root,
no ads, no network permission, no dependencies beyond Android itself (~50 KB).

- Lists installed apps with icon, version, size, and a badge for split installs.
- Tap an app for details: what it's built with (Unity IL2CPP/Mono, Unreal,
  Godot, LÖVE, Flutter, React Native, …), native ABIs, splits, min/target SDK,
  launcher activity, permissions, installer.
- **Extract** saves into a folder you pick once (e.g. `Download/Extracted apps`):
  - single-APK apps as `Label_package_version.apk`
  - split installs as `Label_package_version.apks`: a zip of `base.apk`,
    `split_*.apk` and `info.json`. Reinstall with SAI, or unzip and
    `adb install-multiple *.apk`.
- **Share** extracts and opens the share sheet (send it to Ophelia on Telegram/Discord).
- Long-press to select several apps and extract them in one go.

OBB game-data files (`Android/obb`) can't be read by normal apps on Android 11+.
`ophelia apk pull <package> --obb` gets them over ADB/Shizuku instead.

## Handing apps to Ophelia

```bash
ophelia apk import "/sdcard/Download/Extracted apps"   # Termux: the whole folder
ophelia apk import ~/Downloads/Balatro_com.playstack.balatro.android_1.0.1o.apks
```

Or send her the file and she can import it herself with `apk_extract path=...`.
Then `ophelia apk inspect <package>` (or she uses `apk_inspect`).

## Getting the APK

- **From GitHub:** Actions → "App Extractor (Android)" → latest run → Artifacts →
  `app-extractor-apk`. Unzip and install `app-release.apk` (allow installs from
  your browser/file manager when Android asks).
- **Build it yourself** (JDK 17+, Android SDK with platform 35):

  ```bash
  cd android/apk-extractor
  echo "sdk.dir=$HOME/Android/Sdk" > local.properties   # or set ANDROID_HOME
  ./gradlew assembleRelease        # app/build/outputs/apk/release/app-release.apk
  ./gradlew testDebugUnitTest lintDebug
  adb install app/build/outputs/apk/release/app-release.apk
  ```

The release build is signed with the Android debug key so it installs as-is. If
you ever reinstall from a build made on a different machine, Android will ask
you to uninstall the old one first (different debug key).
