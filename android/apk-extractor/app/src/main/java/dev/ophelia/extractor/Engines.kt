package dev.ophelia.extractor

import java.io.File
import java.util.zip.ZipFile

/**
 * What an app is built with, from the file names inside its APKs (same rules
 * as Ophelia's apk_inspect). Only the zip directory is read, so it's quick
 * even for multi-gigabyte games.
 */
object Engines {
    private val RULES: List<Pair<String, Regex>> = listOf(
        "Unity (IL2CPP)" to """lib/[^/]+/libil2cpp\.so$|assets/bin/Data/Managed/Metadata/global-metadata\.dat$""",
        "Unity (Mono)" to """assets/bin/Data/Managed/[^/]+\.dll$""",
        "Unity" to """lib/[^/]+/libunity\.so$|assets/bin/Data/data\.unity3d$""",
        "Unreal Engine" to """lib/[^/]+/libUE[45]\.so$|lib/[^/]+/libUnreal\.so$""",
        "Godot" to """lib/[^/]+/libgodot_android\.so$|assets/[^/]+\.pck$""",
        "LÖVE (Lua source inside)" to """lib/[^/]+/liblove\.so$|\.love$""",
        "Flutter" to """lib/[^/]+/libflutter\.so$|assets/flutter_assets/""",
        "React Native" to """lib/[^/]+/libreactnativejni\.so$|assets/index\.android\.bundle$""",
        "Cocos2d-x" to """lib/[^/]+/libcocos\w*\.so$""",
        "GameMaker" to """lib/[^/]+/libyoyo\.so$|assets/game\.droid$""",
        "Defold" to """lib/[^/]+/libdmengine\w*\.so$""",
        "Xamarin / .NET" to """lib/[^/]+/libmonodroid\.so$|assemblies/[^/]+\.dll$""",
        "Web app (Cordova/Capacitor)" to """assets/www/index\.html$|assets/public/index\.html$""",
        "Ren'Py" to """lib/[^/]+/librenpython\.so$""",
        "Solar2D" to """lib/[^/]+/libcorona\.so$""",
        "libGDX" to """lib/[^/]+/libgdx\.so$""",
    ).map { (label, rx) -> label to Regex(rx) }

    /** Engine labels matched by any of [paths]; "Unity" is dropped when a
     *  more specific Unity flavour already matched. */
    fun detect(paths: Collection<String>): List<String> {
        val found = RULES.filter { (_, rx) -> paths.any { rx.containsMatchIn(it) } }.map { it.first }
        return if (found.any { it.startsWith("Unity (") }) found - "Unity" else found
    }

    fun entryNames(apks: List<File>): List<String> = apks.flatMap { apk ->
        runCatching {
            ZipFile(apk).use { z -> z.entries().asSequence().map { it.name }.toList() }
        }.getOrDefault(emptyList())
    }

    /** Native ABIs the app ships code for, e.g. [arm64-v8a]. */
    fun abis(paths: Collection<String>): List<String> = paths
        .filter { it.startsWith("lib/") && it.count { c -> c == '/' } >= 2 }
        .map { it.split('/')[1] }
        .distinct()
        .sorted()
}
