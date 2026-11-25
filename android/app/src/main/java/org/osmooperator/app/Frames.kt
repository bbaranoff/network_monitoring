// The frames of the bench, as osmo-egprs-web decodes them (tshark on the node):
// the same source and the same presentation as the postmarketOS app.
//   ws://<node>:8080/   send {"type":"tshark_start"}, receive {"type":"packet","data":{...}}
// The capture is PER CLIENT on the dashboard side: closing the link stops it.
package org.osmooperator.app

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.LinearLayout
import android.widget.TextView
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/** The RR types the dashboard leaves in hex (GSM 04.08 §10.4). */
val RR_NAMES = mapOf(
    0x02 to "SI2bis", 0x03 to "SI2ter", 0x05 to "SI5bis", 0x06 to "SI5ter", 0x0e to "CHANNEL MODE MODIFY",
    0x10 to "CHANNEL MODE MODIFY ACK", 0x13 to "CLASSMARK ENQUIRY", 0x16 to "CLASSMARK CHANGE",
    0x19 to "SI1", 0x1a to "SI2", 0x1b to "SI3", 0x1c to "SI4", 0x1d to "SI5", 0x1e to "SI6", 0x1f to "SI7", 0x18 to "SI8",
    0x22 to "PAGING REQ 2", 0x24 to "PAGING REQ 3", 0x27 to "PAGING RESPONSE", 0x28 to "HANDOVER FAILURE",
    0x2b to "HANDOVER CMD", 0x2c to "HANDOVER COMPLETE", 0x32 to "CIPHERING MODE CMD",
    0x35 to "CIPHERING MODE COMPLETE", 0x30 to "PARTIAL RELEASE", 0x0f to "PARTIAL RELEASE COMPLETE"
)

/** What one wants to see go by without looking for it: outlined row. */
val KEY_MESSAGES = listOf(
    "location updating", "measurement report", "cm service", "paging response", "setup",
    "connect", "disconnect", "release", "authentication", "ciphering mode", "identity",
    "imsi detach", "assignment", "handover", "attach", "tracking area", "cp-data"
)

val FAMILY = mapOf(
    "bcch" to "BCCH", "lapdm" to "LAPDm", "mm" to "MM", "rr" to "RR", "cc" to "CC", "sms" to "SMS",
    "core" to "CŒUR", "lte" to "LTE", "rrc" to "RRC", "nas" to "NAS", "mon" to "MESURE", "other" to "AUTRE"
)

val FAMILY_COLOR = mapOf(
    "bcch" to 0x77767b, "lapdm" to 0x3584e4, "mm" to 0x9141ac, "rr" to 0x26a269, "cc" to 0xe66100,
    "sms" to 0xf5c211, "core" to 0x2190a4, "lte" to 0xcdab8f, "rrc" to 0xc061cb, "nas" to 0xed333b,
    "mon" to 0x5e5c64, "other" to 0x77767b
)

/** Quick filters: broadcast (Paging, SI) drowns the rest. */
val CATEGORIES = listOf(
    "all" to "Tout", "sig" to "Signalisation", "meas" to "Mesures",
    "bcast" to "Diffusion", "sms" to "SMS", "core" to "Cœur"
)

private val PREFIX = Regex("^(\\s*\\([^)]*\\))+\\s*")

class Frame(val d: JSONObject) {
    val num = d.optLong("frameNum", 0)
    val proto = d.optString("protocol").ifEmpty { "?" }
    val direction: String = d.optString("direction").ifEmpty {
        if (d.optBoolean("uplink", false)) "UL" else if (d.optString("arfcn").isNotEmpty()) "DL" else ""
    }
    val info = d.optString("info")
    val where: String
    val detail: String
    val kind: String
    val title: String
    val full: String
    val meta: String
    val cat: String
    val key: Boolean
    val when_: String
    val haystack: String

    init {
        val ts = d.optDouble("ts", System.currentTimeMillis() / 1000.0)
        when_ = SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT).format(Date((ts * 1000).toLong()))
        val w = mutableListOf<String>()
        fun s(k: String) = d.optString(k).takeIf { it.isNotEmpty() && it != "null" }
        s("channel")?.let { w.add(it) }
        s("timeslot")?.let { w.add("TS$it") }
        s("arfcn")?.let { w.add("ARFCN $it") }
        s("sctpPorts")?.let { w.add(it) }
        s("rnti")?.let { w.add("RNTI $it") }
        s("sfn")?.let { w.add("SFN $it") }
        s("opLabel")?.let { if (it != "—" && !it.contains("NaN")) w.add(it) }
        where = w.joinToString("  ")
        var rr = s("rr") ?: ""
        if (rr.startsWith("RR 0x")) {
            rr = rr.substring(5).split(" ")[0].toIntOrNull(16)?.let { RR_NAMES[it] } ?: rr
        }
        val cc = s("cc") ?: ""
        var l3 = s("l3") ?: ""
        if (l3.isNotEmpty() && (cc.isNotEmpty() || rr.isNotEmpty())) l3 += ": " + cc.ifEmpty { rr }
        detail = listOfNotNull(s("l2"), l3.ifEmpty { null }, s("fn")?.let { "FN $it" }, s("length")?.let { "$it o" })
            .joinToString(" · ")
        kind = kindOf(d, proto, l3)
        title = titleOf(info, rr, cc, proto)
        full = PREFIX.replace(info, "").trim()
        meta = listOf(where, if (info.lowercase().startsWith("mon:")) info.substring(4).trim() else "")
            .filter { it.isNotEmpty() }.joinToString("  ·  ")
        val low = "$info $title".lowercase()
        cat = when {
            kind == "mon" || "measurement" in low -> "meas"
            kind == "bcch" -> "bcast"
            kind == "sms" -> "sms"
            kind == "core" || kind == "lte" -> "core"
            else -> "sig"
        }
        key = KEY_MESSAGES.any { it in low }
        haystack = listOf(proto, direction, where, info, detail, s("src") ?: "", s("dst") ?: "").joinToString(" ").lowercase()
    }

    companion object {
        /** The family, for the colour: one per frame, the most telling. */
        fun kindOf(d: JSONObject, proto: String, l3: String): String {
            val info = d.optString("info").lowercase()
            val chan = d.optString("channel")
            return when {
                proto == "NAS-EPS" -> "nas"
                proto == "LTE RRC" -> "rrc"
                proto in setOf("MAC-LTE", "RLC-LTE", "PDCP-LTE") -> "lte"
                proto == "GSM_SMS" || l3.startsWith("SMS") -> "sms"
                d.optString("cc").isNotEmpty() -> "cc"
                l3.startsWith("MM") -> "mm"
                chan == "BCCH" || chan == "CCCH" || "system information" in info || "paging request" in info -> "bcch"
                l3.startsWith("RR") -> "rr"
                proto == "LAPDm" -> "lapdm"
                proto in setOf("S1AP", "M3UA", "SCTP", "DIAMETER", "GTPv2", "GTP", "SCCP", "BSSAP", "BSSMAP") ||
                    d.optString("sctpPorts").isNotEmpty() -> "core"
                info.startsWith("mon:") -> "mon"
                else -> "other"
            }
        }

        /** « Location Updating Request », not « (SDCCH) (MM) Location Updating Request ». */
        fun titleOf(info: String, rr: String, cc: String, proto: String): String {
            if (info.lowercase().startsWith("mon:")) return "Mesure de cellule"
            var t = PREFIX.replace(info, "").trim()
            if ((t.isEmpty() || t.startsWith("RR 0x")) && rr.isNotEmpty() && !rr.startsWith("RR 0x")) t = rr
            if (t.isEmpty() && cc.isNotEmpty()) t = cc
            if (t.length > 48 && ", " in t) t = t.substringBefore(", ")
            return t.ifEmpty { proto.ifEmpty { "Trame" } }
        }
    }
}

/** The WebSocket to the dashboard: one source at a time, restarted on loss. */
class FrameSource(
    private val url: String,
    private val onFrame: (Frame) -> Unit,
    private val onStatus: (String, Boolean) -> Unit
) {
    private val ui = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder().connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS).build()
    private var ws: WebSocket? = null
    @Volatile private var stopped = false
    @Volatile private var lastRx = System.currentTimeMillis()

    // A SILENT LINK IS A DEAD LINK. The bench never stops talking (Paging, SI,
    // measurements): 25 s of silence means the path changed under the socket
    // (4G coming back, fallback route) without closing it. Reconnect.
    private val watchdog = object : Runnable {
        override fun run() {
            if (stopped) return
            if (ws != null && System.currentTimeMillis() - lastRx > 25000) {
                onStatus("lien muet depuis 25 s, on se rebranche…", false)
                ws?.cancel(); ws = null
                lastRx = System.currentTimeMillis()
                connect()
            }
            ui.postDelayed(this, 10000)
        }
    }

    init { connect(); ui.postDelayed(watchdog, 10000) }

    private fun connect() {
        if (stopped) return
        onStatus("connexion à $url…", false)
        client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (stopped) { webSocket.close(1000, null); return }   // stopped during the handshake
                ws = webSocket
                lastRx = System.currentTimeMillis()
                webSocket.send("{\"type\":\"tshark_start\"}")
                ui.post { onStatus("dashboard $url : capture demandée", true) }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (webSocket !== ws) return                    // an old link the watchdog dropped
                lastRx = System.currentTimeMillis()
                val m = try { JSONObject(text) } catch (e: Exception) { return }
                when (m.optString("type")) {
                    "packet" -> {
                        val f = Frame(m.optJSONObject("data") ?: JSONObject())
                        ui.post { if (!stopped) onFrame(f) }
                    }
                    "tshark_stopped" -> {
                        ui.post { onStatus("capture arrêtée côté dashboard, on la redemande", true) }
                        ui.postDelayed({ if (!stopped) ws?.send("{\"type\":\"tshark_start\"}") }, 3000)
                    }
                    "tshark_status" -> if (m.optJSONObject("data")?.optBoolean("active", true) == false)
                        ui.postDelayed({ if (!stopped) ws?.send("{\"type\":\"tshark_start\"}") }, 3000)
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = lost(webSocket)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!stopped && (ws == null || webSocket === ws))
                    ui.post { onStatus("dashboard injoignable ($url) : ${t.message ?: t.javaClass.simpleName}", false) }
                lost(webSocket)
            }
        })
    }

    private fun lost(which: WebSocket) {
        if (ws != null && which !== ws) return                  // not the current link
        ws = null
        if (!stopped) ui.postDelayed({ if (ws == null) connect() }, 5000)
    }

    fun stop() {
        stopped = true
        ws?.close(1000, null)
        ws = null
        client.dispatcher.executorService.shutdown()
    }
}

/** One row: the message name big, family / direction pills, where, detail. */
class FrameAdapter(private val ctx: Context, private val dark: Boolean) : BaseAdapter() {
    private val ink = if (dark) Color.WHITE else Color.rgb(36, 31, 49)
    private fun ink(a: Int) = Color.argb(a, Color.red(ink), Color.green(ink), Color.blue(ink))
    private val paper = if (dark) Color.rgb(30, 30, 34) else Color.rgb(250, 250, 251)
    private val all = ArrayList<Frame>()
    private var shown: List<Frame> = emptyList()
    var category = "all"
        set(v) { field = v; refilter() }
    var query = ""
        set(v) { field = v.lowercase(); refilter() }

    fun add(f: Frame, max: Int = 2000) {
        all.add(f)
        if (all.size > max) all.subList(0, all.size - max).clear()
        refilter()
    }

    fun clear() { all.clear(); refilter() }
    fun total() = all.size

    private fun refilter() {
        val words = query.split(" ").filter { it.isNotEmpty() }
        shown = all.filter { f -> (category == "all" || f.cat == category) && words.all { it in f.haystack } }
        notifyDataSetChanged()
    }

    override fun getCount() = shown.size
    override fun getItem(position: Int) = shown[position]
    override fun getItemId(position: Int) = shown[position].num

    private fun dp(v: Float) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, ctx.resources.displayMetrics)

    private class Holder(
        val root: LinearLayout, val title: TextView, val time: TextView, val fam: TextView,
        val dir: TextView, val where: TextView, val detail: TextView
    )

    private fun pill(bg: Int, fg: Int): TextView = TextView(ctx).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(fg)
        setPadding(dp(8f).toInt(), dp(1f).toInt(), dp(8f).toInt(), dp(1f).toInt())
        background = GradientDrawable().apply { cornerRadius = dp(99f); setColor(bg) }
    }

    override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
        val h = (convertView?.tag as? Holder) ?: run {
            val root = LinearLayout(ctx).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(14f).toInt(), dp(8f).toInt(), dp(12f).toInt(), dp(8f).toInt())
            }
            val top = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
            val title = TextView(ctx).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f); typeface = Typeface.DEFAULT_BOLD
                setTextColor(ink); maxLines = 1; ellipsize = TextUtils.TruncateAt.END
            }
            val time = TextView(ctx).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f); typeface = Typeface.MONOSPACE
                setTextColor(ink(150))
            }
            top.addView(title, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            top.addView(time)
            val mid = LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(3f).toInt(), 0, dp(2f).toInt())
            }
            val fam = pill(Color.GRAY, Color.WHITE)
            val dir = pill(Color.TRANSPARENT, Color.WHITE)
            val where = TextView(ctx).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f); setTextColor(ink(190))
                maxLines = 1; ellipsize = TextUtils.TruncateAt.END
            }
            val gap = { LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { marginEnd = dp(6f).toInt() } }
            mid.addView(fam, gap()); mid.addView(dir, gap())
            mid.addView(where, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            val detail = TextView(ctx).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f); setTextColor(ink(140))
                maxLines = 1; ellipsize = TextUtils.TruncateAt.END
            }
            root.addView(top); root.addView(mid); root.addView(detail)
            Holder(root, title, time, fam, dir, where, detail).also { root.tag = it }
        }
        val f = shown[position]
        val c = 0xff000000.toInt() or (FAMILY_COLOR[f.kind] ?: 0x77767b)
        val quiet = f.kind == "bcch" || f.kind == "mon" || f.kind == "other"
        // left border in the family colour, light tint, outline on key messages
        val tint = Color.argb(if (quiet) 18 else 38, Color.red(c), Color.green(c), Color.blue(c))
        h.root.background = android.graphics.drawable.LayerDrawable(arrayOf(
            GradientDrawable().apply { setColor(c) },
            GradientDrawable().apply {
                setColor(paper); if (f.key) setStroke(dp(1f).toInt(), Color.argb(110, 53, 132, 228))
            },
            GradientDrawable().apply { setColor(tint) }
        )).apply {
            setLayerInset(1, dp(4f).toInt(), 0, 0, dp(1f).toInt())
            setLayerInset(2, dp(4f).toInt(), 0, 0, dp(1f).toInt())
        }
        h.title.text = f.title
        h.title.alpha = if (quiet) 0.8f else 1f
        h.time.text = f.when_.substring(0, 12)
        h.fam.text = FAMILY[f.kind] ?: f.proto
        h.fam.setTextColor(if (f.kind == "sms") Color.rgb(36, 31, 49) else Color.WHITE)
        (h.fam.background as GradientDrawable).setColor(c)
        when (f.direction) {
            "UL" -> {
                h.dir.text = "↑ MS → réseau"; h.dir.setTextColor(if (dark) Color.rgb(246, 211, 45) else Color.rgb(156, 110, 3))
                (h.dir.background as GradientDrawable).setColor(Color.argb(46, 229, 165, 10)); h.dir.visibility = View.VISIBLE
            }
            "DL" -> {
                h.dir.text = "↓ réseau → MS"; h.dir.setTextColor(if (dark) Color.rgb(153, 193, 241) else Color.rgb(28, 113, 216))
                (h.dir.background as GradientDrawable).setColor(Color.argb(51, 53, 132, 228)); h.dir.visibility = View.VISIBLE
            }
            else -> h.dir.visibility = View.GONE
        }
        h.where.text = f.meta
        val rest = if (f.full.isNotEmpty() && f.full != f.title && !f.info.lowercase().startsWith("mon:")) f.full else ""
        val det = listOf(rest, f.detail).filter { it.isNotEmpty() }.joinToString(" · ")
        h.detail.text = det
        h.detail.visibility = if (det.isEmpty()) View.GONE else View.VISIBLE
        return h.root
    }
}
