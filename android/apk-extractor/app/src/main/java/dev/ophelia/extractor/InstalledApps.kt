package dev.ophelia.extractor

import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import java.io.File

data class AppEntry(
    val label: String,
    val pkg: String,
    val versionName: String?,
    val versionCode: Long,
    val system: Boolean,
    val baseApk: File,
    val splits: List<File>,
    val updatedAt: Long,
    val info: ApplicationInfo,
) {
    val apks: List<File> get() = listOf(baseApk) + splits
    val size: Long get() = apks.sumOf { it.length() }
    val isBundle: Boolean get() = splits.isNotEmpty()
}

object InstalledApps {
    @Suppress("DEPRECATION")
    fun load(pm: PackageManager): List<AppEntry> {
        val packages: List<PackageInfo> = if (Build.VERSION.SDK_INT >= 33) {
            pm.getInstalledPackages(PackageManager.PackageInfoFlags.of(0))
        } else {
            pm.getInstalledPackages(0)
        }
        return packages.mapNotNull { p ->
            val ai = p.applicationInfo ?: return@mapNotNull null
            val base = File(ai.sourceDir ?: return@mapNotNull null)
            AppEntry(
                label = ai.loadLabel(pm).toString(),
                pkg = p.packageName,
                versionName = p.versionName,
                versionCode = if (Build.VERSION.SDK_INT >= 28) p.longVersionCode else p.versionCode.toLong(),
                system = ai.flags and (ApplicationInfo.FLAG_SYSTEM or ApplicationInfo.FLAG_UPDATED_SYSTEM_APP) != 0,
                baseApk = base,
                splits = ai.splitSourceDirs.orEmpty().map(::File),
                updatedAt = p.lastUpdateTime,
                info = ai,
            )
        }.sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.label })
    }

    /** Longer description for the details dialog (the slow parts: reading
     *  the zip directories, permissions and activities). */
    @Suppress("DEPRECATION")
    fun describe(pm: PackageManager, app: AppEntry): String {
        val flags = PackageManager.GET_PERMISSIONS or PackageManager.GET_ACTIVITIES
        val p = runCatching {
            if (Build.VERSION.SDK_INT >= 33) {
                pm.getPackageInfo(app.pkg, PackageManager.PackageInfoFlags.of(flags.toLong()))
            } else {
                pm.getPackageInfo(app.pkg, flags)
            }
        }.getOrNull()
        val names = Engines.entryNames(app.apks)
        val engines = Engines.detect(names)
        val abis = Engines.abis(names)
        val launcher = pm.getLaunchIntentForPackage(app.pkg)?.component?.className
        val installer = runCatching {
            if (Build.VERSION.SDK_INT >= 30) pm.getInstallSourceInfo(app.pkg).installingPackageName
            else pm.getInstallerPackageName(app.pkg)
        }.getOrNull()
        return buildString {
            appendLine("${app.pkg}")
            appendLine("Version ${app.versionName ?: "?"} (${app.versionCode})")
            appendLine("Built with: ${engines.joinToString().ifEmpty { "plain Android (Java/Kotlin)" }}")
            if (abis.isNotEmpty()) appendLine("Native code: ${abis.joinToString()}")
            appendLine("Size: ${Format.size(app.size)} in ${app.apks.size} APK file(s)")
            if (app.isBundle) appendLine("Splits: ${app.splits.joinToString { it.name }}")
            appendLine("minSdk ${app.info.minSdkVersion} · targetSdk ${app.info.targetSdkVersion}")
            if (app.info.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) appendLine("DEBUGGABLE")
            launcher?.let { appendLine("Launcher: $it") }
            p?.activities?.let { appendLine("Screens (activities): ${it.size}") }
            p?.requestedPermissions?.let { perms ->
                appendLine("Permissions (${perms.size}): " +
                    perms.joinToString { it.removePrefix("android.permission.") })
            }
            installer?.let { appendLine("Installed by: $it") }
            append("Files: ${names.size} · ${app.baseApk.parent}")
        }
    }
}

object Format {
    fun size(bytes: Long): String {
        var v = bytes.toDouble()
        for (unit in listOf("B", "KB", "MB", "GB")) {
            if (v < 1024 || unit == "GB") return if (unit == "B") "${bytes} B" else "%.1f %s".format(v, unit)
            v /= 1024
        }
        return "$bytes B"
    }
}
