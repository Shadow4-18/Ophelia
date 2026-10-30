package dev.ophelia.extractor

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle as OsBundle
import android.provider.DocumentsContract
import android.provider.Settings
import android.view.ActionMode
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.AbsListView
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.SearchView
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var adapter: AppAdapter
    private lateinit var list: ListView
    private lateinit var status: TextView
    private lateinit var loading: ProgressBar
    private val prefs by lazy { getSharedPreferences("extractor", MODE_PRIVATE) }

    // Waiting for the folder picker before extracting these.
    private var pending: List<AppEntry> = emptyList()
    private var pendingShare = false

    override fun onCreate(savedInstanceState: OsBundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        list = findViewById(R.id.list)
        status = findViewById(R.id.status)
        loading = findViewById(R.id.loading)
        adapter = AppAdapter(this)
        adapter.setShowSystem(prefs.getBoolean(PREF_SYSTEM, false))
        list.adapter = adapter
        list.setOnItemClickListener { _, _, pos, _ -> showDetails(adapter.getItem(pos)) }
        list.choiceMode = ListView.CHOICE_MODE_MULTIPLE_MODAL
        list.setMultiChoiceModeListener(selection)
        loadApps()
    }

    private fun loadApps() {
        loading.visibility = View.VISIBLE
        status.setText(R.string.loading)
        thread(name = "load-apps") {
            val apps = InstalledApps.load(packageManager)
            runOnUiThread {
                adapter.setApps(apps)
                loading.visibility = View.GONE
                updateStatus()
            }
        }
    }

    private fun updateStatus() {
        val folder = folder()?.let { folderName(it) } ?: getString(R.string.no_folder)
        status.text = resources.getQuantityString(R.plurals.status, adapter.count, adapter.count, folder)
    }

    // --- menu ---------------------------------------------------------------

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main, menu)
        (menu.findItem(R.id.search).actionView as SearchView).apply {
            queryHint = getString(R.string.search_hint)
            setOnQueryTextListener(object : SearchView.OnQueryTextListener {
                override fun onQueryTextSubmit(q: String) = true
                override fun onQueryTextChange(q: String): Boolean {
                    adapter.setQuery(q)
                    updateStatus()
                    return true
                }
            })
        }
        menu.findItem(R.id.show_system).isChecked = prefs.getBoolean(PREF_SYSTEM, false)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        R.id.show_system -> {
            item.isChecked = !item.isChecked
            prefs.edit().putBoolean(PREF_SYSTEM, item.isChecked).apply()
            adapter.setShowSystem(item.isChecked)
            updateStatus()
            true
        }
        R.id.choose_folder -> { pickFolder(); true }
        R.id.refresh -> { loadApps(); true }
        R.id.about -> {
            AlertDialog.Builder(this).setTitle(R.string.app_name)
                .setMessage(R.string.about_text).setPositiveButton(android.R.string.ok, null).show()
            true
        }
        else -> super.onOptionsItemSelected(item)
    }

    private val selection = object : AbsListView.MultiChoiceModeListener {
        override fun onCreateActionMode(mode: ActionMode, menu: Menu): Boolean {
            mode.menuInflater.inflate(R.menu.selection, menu)
            return true
        }

        override fun onPrepareActionMode(mode: ActionMode, menu: Menu) = false

        override fun onItemCheckedStateChanged(mode: ActionMode, position: Int, id: Long, checked: Boolean) {
            mode.title = resources.getQuantityString(R.plurals.selected, list.checkedItemCount, list.checkedItemCount)
        }

        override fun onActionItemClicked(mode: ActionMode, item: MenuItem): Boolean {
            when (item.itemId) {
                R.id.extract_selected -> {
                    val checked = list.checkedItemPositions
                    val apps = (0 until adapter.count).filter { checked[it] }.map { adapter.getItem(it) }
                    mode.finish()
                    extract(apps, share = false)
                }
                R.id.select_all -> for (i in 0 until adapter.count) list.setItemChecked(i, true)
                else -> return false
            }
            return true
        }

        override fun onDestroyActionMode(mode: ActionMode) {}
    }

    // --- details --------------------------------------------------------------

    private fun showDetails(app: AppEntry) {
        val dialog = AlertDialog.Builder(this)
            .setTitle(app.label)
            .setIcon(app.info.loadIcon(packageManager))
            .setMessage(R.string.reading_app)
            .setPositiveButton(R.string.extract) { _, _ -> extract(listOf(app), share = false) }
            .setNeutralButton(R.string.share) { _, _ -> extract(listOf(app), share = true) }
            .setNegativeButton(R.string.app_settings) { _, _ ->
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${app.pkg}")))
            }
            .show()
        thread(name = "describe") {
            val text = runCatching { InstalledApps.describe(packageManager, app) }
                .getOrElse { getString(R.string.read_failed, it.message) }
            runOnUiThread { if (dialog.isShowing) dialog.setMessage(text) }
        }
    }

    // --- output folder ------------------------------------------------------

    private fun folder(): Uri? {
        val uri = prefs.getString(PREF_TREE, null)?.let(Uri::parse) ?: return null
        val granted = contentResolver.persistedUriPermissions.any { it.uri == uri && it.isWritePermission }
        return if (granted) uri else null
    }

    private fun folderName(tree: Uri): String =
        DocumentsContract.getTreeDocumentId(tree).substringAfter(':').ifEmpty { "/" }

    private fun pickFolder() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        startActivityForResult(intent, REQ_FOLDER)
        Toast.makeText(this, R.string.pick_folder_hint, Toast.LENGTH_LONG).show()
    }

    @Deprecated("Framework Activity API; this app has no AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_FOLDER) return
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) {
            pending = emptyList()
            return
        }
        contentResolver.takePersistableUriPermission(
            uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
        prefs.edit().putString(PREF_TREE, uri.toString()).apply()
        updateStatus()
        if (pending.isNotEmpty()) {
            val apps = pending
            pending = emptyList()
            extract(apps, pendingShare)
        }
    }

    // --- extracting -----------------------------------------------------------

    private fun extract(apps: List<AppEntry>, share: Boolean) {
        if (apps.isEmpty()) return
        val tree = folder()
        if (tree == null) {
            pending = apps
            pendingShare = share
            pickFolder()
            return
        }
        val total = apps.sumOf { it.size }.coerceAtLeast(1)
        val done = AtomicLong()
        val cancel = AtomicBoolean()
        val view = layoutInflater.inflate(R.layout.dialog_progress, null)
        val bar = view.findViewById<ProgressBar>(R.id.progress)
        val label = view.findViewById<TextView>(R.id.progress_label)
        bar.max = 1000
        val dialog = AlertDialog.Builder(this)
            .setTitle(if (apps.size == 1) getString(R.string.extracting_one, apps[0].label)
                      else resources.getQuantityString(R.plurals.extracting_many, apps.size, apps.size))
            .setView(view)
            .setCancelable(false)
            .setNegativeButton(android.R.string.cancel) { _, _ -> cancel.set(true) }
            .show()

        thread(name = "extract") {
            val extractor = Extractor(contentResolver, tree)
            val saved = mutableListOf<Pair<AppEntry, Uri>>()
            val failed = mutableListOf<String>()
            var lastUi = 0L
            for (app in apps) {
                if (cancel.get()) break
                runOnUiThread { label.text = app.label }
                try {
                    saved += app to extractor.extract(app, { n ->
                        val now = done.addAndGet(n)
                        if (System.nanoTime() - lastUi > 100_000_000) {
                            lastUi = System.nanoTime()
                            runOnUiThread {
                                bar.progress = (now * 1000 / total).toInt()
                                label.text = getString(R.string.progress_bytes, app.label,
                                    Format.size(now), Format.size(total))
                            }
                        }
                    }, { cancel.get() })
                } catch (_: Extractor.Cancelled) {
                    break
                } catch (e: Exception) {
                    failed += "${app.label}: ${e.message ?: e.javaClass.simpleName}"
                }
            }
            runOnUiThread {
                dialog.dismiss()
                finished(tree, saved, failed, cancel.get(), share)
            }
        }
    }

    private fun finished(
        tree: Uri,
        saved: List<Pair<AppEntry, Uri>>,
        failed: List<String>,
        cancelled: Boolean,
        share: Boolean,
    ) {
        if (share && saved.size == 1 && failed.isEmpty()) {
            val (app, uri) = saved[0]
            val send = Intent(Intent.ACTION_SEND)
                .setType(if (app.isBundle) Bundle.APKS_MIME else Bundle.APK_MIME)
                .putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            startActivity(Intent.createChooser(send, getString(R.string.share_title, app.label)))
            return
        }
        val msg = buildString {
            if (saved.isNotEmpty()) {
                appendLine(resources.getQuantityString(R.plurals.saved_to, saved.size, saved.size, folderName(tree)))
                saved.forEach { (app, _) ->
                    appendLine("• " + Bundle.fileName(app.label, app.pkg, app.versionName, app.isBundle))
                }
            }
            if (cancelled) appendLine(getString(R.string.cancelled))
            if (failed.isNotEmpty()) {
                appendLine(getString(R.string.failed))
                failed.forEach { appendLine("• $it") }
            }
            if (saved.isNotEmpty()) {
                appendLine()
                append(getString(R.string.ophelia_hint, folderName(tree)))
            }
        }
        AlertDialog.Builder(this).setTitle(R.string.done).setMessage(msg.trim())
            .setPositiveButton(android.R.string.ok, null).show()
    }

    companion object {
        private const val REQ_FOLDER = 1
        private const val PREF_TREE = "tree"
        private const val PREF_SYSTEM = "show_system"
    }
}
