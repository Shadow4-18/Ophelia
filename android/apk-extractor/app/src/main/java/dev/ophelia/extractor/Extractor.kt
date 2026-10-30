package dev.ophelia.extractor

import android.content.ContentResolver
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import java.io.IOException

/** Saves apps into the folder the user picked (Storage Access Framework). */
class Extractor(private val resolver: ContentResolver, private val tree: Uri) {

    class Cancelled : Exception()

    /** Write [app] into the folder; returns the new document's Uri. */
    fun extract(app: AppEntry, progress: (Long) -> Unit, cancelled: () -> Boolean): Uri {
        val name = Bundle.fileName(app.label, app.pkg, app.versionName, app.isBundle)
        val mime = if (app.isBundle) Bundle.APKS_MIME else Bundle.APK_MIME
        val parent = DocumentsContract.buildDocumentUriUsingTree(tree, DocumentsContract.getTreeDocumentId(tree))
        val doc = DocumentsContract.createDocument(resolver, parent, mime, name)
            ?: throw IOException("Couldn't create $name in that folder")
        try {
            val out = resolver.openOutputStream(doc, "w") ?: throw IOException("Couldn't write $name")
            val ok = out.use {
                if (app.isBundle) {
                    Bundle.writeApks(Bundle.sources(app.baseApk, app.splits), info(app), it, progress, cancelled)
                } else {
                    app.baseApk.inputStream().use { input -> Bundle.copy(input, it, progress, cancelled) }
                }
            }
            if (!ok) throw Cancelled()
            return doc
        } catch (e: Exception) {
            runCatching { DocumentsContract.deleteDocument(resolver, doc) }
            throw e
        }
    }

    private fun info(app: AppEntry) = Bundle.infoJson(
        linkedMapOf(
            "package" to app.pkg,
            "label" to app.label,
            "version_name" to app.versionName,
            "version_code" to app.versionCode,
            "files" to app.apks.map { if (it == app.baseApk) "base.apk" else it.name },
            "device" to "${Build.MANUFACTURER} ${Build.MODEL}",
            "android_sdk" to Build.VERSION.SDK_INT,
            "extracted_at" to System.currentTimeMillis() / 1000,
            "source" to "Ophelia App Extractor",
        )
    )
}
