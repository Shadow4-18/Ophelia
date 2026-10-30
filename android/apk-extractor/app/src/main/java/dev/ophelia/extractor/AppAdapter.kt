package dev.ophelia.extractor

import android.content.Context
import android.content.pm.PackageManager
import android.graphics.drawable.Drawable
import android.os.Handler
import android.os.Looper
import android.util.LruCache
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.ImageView
import android.widget.TextView
import java.util.concurrent.Executors

class AppAdapter(context: Context) : BaseAdapter() {
    private val inflater = LayoutInflater.from(context)
    private val pm: PackageManager = context.packageManager
    private val icons = LruCache<String, Drawable>(150)
    private val iconLoader = Executors.newFixedThreadPool(2)
    private val main = Handler(Looper.getMainLooper())

    private var all: List<AppEntry> = emptyList()
    private var shown: List<AppEntry> = emptyList()
    private var query = ""
    private var showSystem = false

    fun setApps(apps: List<AppEntry>) {
        all = apps
        refilter()
    }

    fun setQuery(q: String) {
        query = q.trim()
        refilter()
    }

    fun setShowSystem(on: Boolean) {
        showSystem = on
        refilter()
    }

    private fun refilter() {
        shown = all.filter { a ->
            (showSystem || !a.system) &&
                (query.isEmpty() || a.label.contains(query, true) || a.pkg.contains(query, true))
        }
        notifyDataSetChanged()
    }

    override fun getCount() = shown.size
    override fun getItem(position: Int): AppEntry = shown[position]
    override fun getItemId(position: Int) = shown[position].pkg.hashCode().toLong()
    override fun hasStableIds() = true

    private class Holder(v: View) {
        val icon: ImageView = v.findViewById(R.id.icon)
        val label: TextView = v.findViewById(R.id.label)
        val detail: TextView = v.findViewById(R.id.detail)
        val badge: TextView = v.findViewById(R.id.badge)
        var pkg = ""
    }

    override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
        val view = convertView ?: inflater.inflate(R.layout.item_app, parent, false).also { it.tag = Holder(it) }
        val h = view.tag as Holder
        val app = shown[position]
        h.pkg = app.pkg
        h.label.text = app.label
        h.detail.text = listOfNotNull(app.pkg, app.versionName, Format.size(app.size)).joinToString(" · ")
        h.badge.visibility = if (app.isBundle) View.VISIBLE else View.GONE
        h.badge.text = view.resources.getQuantityString(R.plurals.badge_split, app.apks.size, app.apks.size)
        val cached = icons.get(app.pkg)
        h.icon.setImageDrawable(cached)
        if (cached == null) {
            iconLoader.execute {
                val d = runCatching { app.info.loadIcon(pm) }.getOrNull() ?: return@execute
                icons.put(app.pkg, d)
                main.post { if (h.pkg == app.pkg) h.icon.setImageDrawable(d) }
            }
        }
        return view
    }
}
