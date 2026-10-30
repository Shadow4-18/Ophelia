package dev.ophelia.extractor

import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.util.zip.Deflater
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * Writing an extracted app. A single-APK app is copied as-is (.apk); a split
 * install becomes an .apks bundle: a zip of base.apk + split_*.apk + info.json,
 * which SAI, `adb install-multiple` (after unzipping) and Ophelia's
 * `ophelia apk import` all understand.
 */
object Bundle {
    const val APK_MIME = "application/vnd.android.package-archive"
    const val APKS_MIME = "application/octet-stream"

    data class Source(val name: String, val file: File)

    fun fileName(label: String, pkg: String, versionName: String?, bundle: Boolean): String {
        val clean = { s: String -> s.replace(Regex("""[^\p{L}\p{N}._-]+"""), "_").trim('_', '.') }
        val parts = listOf(clean(label), clean(pkg), clean(versionName ?: "")).filter { it.isNotEmpty() }
        return parts.joinToString("_").take(120) + if (bundle) ".apks" else ".apk"
    }

    /** base.apk keeps its name; splits keep theirs (split_config.arm64_v8a.apk). */
    fun sources(baseApk: File, splits: List<File>): List<Source> =
        listOf(Source("base.apk", baseApk)) + splits.map { Source(it.name, it) }

    fun infoJson(fields: Map<String, Any?>): String = fields.entries.joinToString(
        prefix = "{\n", separator = ",\n", postfix = "\n}\n",
    ) { (k, v) -> "  ${quote(k)}: ${value(v)}" }

    private fun value(v: Any?): String = when (v) {
        null -> "null"
        is Number, is Boolean -> v.toString()
        is Collection<*> -> v.joinToString(prefix = "[", postfix = "]") { value(it) }
        else -> quote(v.toString())
    }

    private fun quote(s: String): String = buildString {
        append('"')
        for (c in s) when {
            c == '"' -> append("\\\"")
            c == '\\' -> append("\\\\")
            c == '\n' -> append("\\n")
            c < ' ' -> append("\\u%04x".format(c.code))
            else -> append(c)
        }
        append('"')
    }

    /** Copy one stream to another, reporting bytes as they go. Returns false
     *  if [cancelled] stopped it. */
    fun copy(input: InputStream, out: OutputStream, progress: (Long) -> Unit, cancelled: () -> Boolean): Boolean {
        val buf = ByteArray(1 shl 16)
        while (true) {
            if (cancelled()) return false
            val n = input.read(buf)
            if (n < 0) return true
            out.write(buf, 0, n)
            progress(n.toLong())
        }
    }

    /** Write the .apks zip. APKs are already compressed, so entries are
     *  stored at the fastest deflate level rather than squeezed again. */
    fun writeApks(
        sources: List<Source>,
        info: String,
        out: OutputStream,
        progress: (Long) -> Unit = {},
        cancelled: () -> Boolean = { false },
    ): Boolean {
        ZipOutputStream(out.buffered()).use { zip ->
            zip.setLevel(Deflater.BEST_SPEED)
            for (src in sources) {
                zip.putNextEntry(ZipEntry(src.name).apply { time = src.file.lastModified() })
                val ok = src.file.inputStream().use { copy(it, zip, progress, cancelled) }
                zip.closeEntry()
                if (!ok) return false
            }
            zip.putNextEntry(ZipEntry("info.json"))
            zip.write(info.toByteArray())
            zip.closeEntry()
        }
        return true
    }
}
