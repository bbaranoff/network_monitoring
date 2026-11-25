// Network Monitor for Android: the twin of network-monitor.py (postmarketOS).
// A phone of the bench that needs no SIM, the radio seen by Android, and the
// node itself (spectrum, services, VTY console, outbound trunk).
//
// EVERYTHING STARTS FROM ONE ADDRESS, the node (local IP, WireGuard, VPS),
// typed once in "Compte":
//   http://<node>:8081/psd?src=bts|ms   I/Q spectrum of the bench (fft-web)
//   http://<node>:8090/api/etat         processes and VTYs (osmo-reglages)
//   http://<node>:8090/api/console      one VTY command
//   http://<node>:8090/api/trunk        outbound trunk: zadarma | dongle
//   http://<node>:8088/inscription      invitation code -> SIP account
//   sip:<node>:5060                     calls and SMS (SIP MESSAGE), Linphone SDK
// None of it is authenticated on the node side: LAN or WireGuard only.
//
// THE ENROLMENT CONTRACT:
//   POST /inscription   {"code": "...", "pseudo": "..."}
//   200  {"ok": true, "msisdn": "100150", "sip_user": "...", "sip_password": "...",
//         "sip_domain": "host", "sip_port": 5060, "sip_transport": "udp"}
//   else {"ok": false, "error": "..."}
//
//   ws://<node>:8080/                   the frames (dashboard, tshark on the node)
//
// MODES: Basique (phone, messages, account) / Medium (+ radio: cell, frames,
// journal; node: spectra, services) / Expert (+ VTY console, trunk choice).
//
// v0.4: same pages as the postmarketOS app - Messages (bubbles), Radio in three
// panes (Cellule, Trames, Journal), Node one pane at a time, light theme by
// default, "Forcer la 2G" (carrier privileges, else the hidden radio-info
// screen). The app holds the line only while it is open (no background service).
package org.osmooperator.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.telephony.CellIdentityNr
import android.telephony.CellInfoGsm
import android.telephony.CellInfoLte
import android.telephony.CellInfoNr
import android.telephony.CellInfoWcdma
import android.telephony.CellSignalStrengthNr
import android.telephony.TelephonyManager
import android.provider.Settings
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import java.security.MessageDigest
import org.json.JSONArray
import org.json.JSONObject
import org.linphone.core.Account
import org.linphone.core.Call
import org.linphone.core.ChatMessage
import org.linphone.core.ChatRoom
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.MediaEncryption
import org.linphone.core.RegistrationState
import org.linphone.core.TransportType
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var core: Core
    private lateinit var prefs: SharedPreferences
    private val ui = Handler(Looper.getMainLooper())

    private lateinit var status: TextView
    private lateinit var journal: TextView
    private lateinit var user: EditText
    private lateinit var secret: EditText
    private lateinit var number: EditText
    private lateinit var journalRadio: TextView
    private lateinit var callCard: View
    private lateinit var callPeer: TextView
    private lateinit var callInfo: TextView
    private lateinit var msgTo: EditText
    private lateinit var msgText: EditText
    private lateinit var msgList: LinearLayout
    private lateinit var msgScroll: ScrollView
    private lateinit var framesStatus: TextView
    private lateinit var framesList: ListView
    private lateinit var frames: FrameAdapter
    private lateinit var netState: TextView
    private lateinit var nodeTitle: TextView
    private lateinit var themeState: TextView
    private lateinit var btnAnswer: Button
    private lateinit var btnHangup: Button
    private lateinit var node: EditText
    private lateinit var spectrum: SpectrumView
    private lateinit var services: TextView
    private lateinit var vtySel: Spinner
    private lateinit var vtyCmd: EditText
    private lateinit var vtyOut: TextView
    private lateinit var trunkState: TextView
    private lateinit var modeState: TextView
    private lateinit var account: TextView
    private lateinit var radioRat: TextView
    private lateinit var radioMetric: TextView
    private lateinit var radioCells: TextView

    private var tab = "phone"
    private var radioPage = "cell"          // cell | frames | log
    private var nodePage = 0                // index in nodePages
    private var frameSource: FrameSource? = null
    private var framesHost = ""
    private var framesPaused = false
    private val framesPending = ArrayList<Frame>()
    private var framesNote = ""
    private var callSince = 0L
    private var dark = false
    private val catButtons = HashMap<String, Button>()
    private var psdBusy = false
    private var etatAt = 0L
    private var cellsAt = 0L
    private var vty = JSONArray()
    private var online = "Hors ligne"

    private val host get() = prefs.getString("node", "") ?: ""
    private val mode get() = prefs.getInt("mode", 1)
    private val enrolled get() = prefs.contains("sip_user")
    private val nodePages get() = if (mode == 2) listOf("bts", "ms", "services", "console") else listOf("bts", "ms", "services")
    private val NODE_TITLES = mapOf("bts" to "Spectre BTS (descendant)", "ms" to "Spectre MS (montant)",
        "services" to "Services", "console" to "Console VTY")

    // ── SIP events ───────────────────────────────────────────────────────────
    private val listener = object : CoreListenerStub() {
        override fun onAccountRegistrationStateChanged(
            core: Core, account: Account, state: RegistrationState?, message: String
        ) {
            online = when (state) {
                RegistrationState.Ok -> "En ligne"
                RegistrationState.Progress -> "Connexion au nœud…"
                RegistrationState.Failed -> "Refusé par le nœud : $message"
                else -> "Hors ligne"
            }
            showStatus()
        }

        override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
            val peer = call.remoteAddress.username ?: call.remoteAddress.asStringUriOnly()
            when (state) {
                Call.State.IncomingReceived -> {
                    log("Appel entrant de $peer")
                    btnAnswer.isEnabled = true
                    btnHangup.isEnabled = true
                    showCall(peer, "Appel entrant")
                    show("phone")
                }
                Call.State.OutgoingProgress -> {
                    log("Appel vers $peer…")
                    btnHangup.isEnabled = true
                    showCall(peer, "Appel en cours…")
                }
                Call.State.OutgoingRinging -> showCall(peer, "Ça sonne…")
                Call.State.StreamsRunning -> {
                    log("En communication avec $peer")
                    btnAnswer.isEnabled = false
                    if (callSince == 0L) callSince = SystemClock.elapsedRealtime()
                    showCall(peer, "En communication")
                }
                Call.State.Error -> { log("Appel échoué : $message"); showCall(null, "") }
                Call.State.End, Call.State.Released -> {
                    if (state == Call.State.End) log("Appel terminé")
                    btnAnswer.isEnabled = false
                    btnHangup.isEnabled = core.callsNb > 0
                    if (core.callsNb == 0) showCall(null, "")
                }
                else -> {}
            }
        }

        override fun onMessageReceived(core: Core, chatRoom: ChatRoom, message: ChatMessage) {
            val from = message.fromAddress.username ?: "?"
            val body = message.utf8Text ?: ""
            log("SMS de $from : $body")
            bubble(from, body, false)
            if (msgTo.text.isBlank()) msgTo.setText(from)
        }
    }

    private val poll = object : Runnable {
        override fun run() {
            tick()
            ui.postDelayed(this, 300)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        // The theme, before anything is drawn: light by default (0), dark (1),
        // as the system (2). Changing it recreates the activity.
        prefs = getSharedPreferences("compte", MODE_PRIVATE)
        val th = prefs.getInt("theme", 0)
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
        dark = th == 1 || (th == 2 && night)
        setTheme(if (dark) android.R.style.Theme_Material_NoActionBar else android.R.style.Theme_Material_Light_NoActionBar)
        super.onCreate(savedInstanceState)
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        setContentView(R.layout.main)

        status = findViewById(R.id.status)
        journal = findViewById(R.id.journal)
        user = findViewById(R.id.user)
        secret = findViewById(R.id.secret)
        number = findViewById(R.id.number)
        journalRadio = findViewById(R.id.journal_radio)
        callCard = findViewById(R.id.call_card)
        callPeer = findViewById(R.id.call_peer)
        callInfo = findViewById(R.id.call_info)
        msgTo = findViewById(R.id.msg_to)
        msgText = findViewById(R.id.msg_text)
        msgList = findViewById(R.id.msg_list)
        msgScroll = findViewById(R.id.msg_scroll)
        framesStatus = findViewById(R.id.frames_status)
        framesList = findViewById(R.id.frames_list)
        netState = findViewById(R.id.net_state)
        nodeTitle = findViewById(R.id.node_title)
        themeState = findViewById(R.id.theme_state)
        frames = FrameAdapter(this, dark)
        framesList.adapter = frames
        callCard.background = GradientDrawable().apply { cornerRadius = 48f; setColor(Color.argb(46, 38, 162, 105)) }
        btnAnswer = findViewById(R.id.btn_answer)
        btnHangup = findViewById(R.id.btn_hangup)
        node = findViewById(R.id.node)
        spectrum = findViewById(R.id.spectrum)
        services = findViewById(R.id.services)
        vtySel = findViewById(R.id.vty_sel)
        vtyCmd = findViewById(R.id.vty_cmd)
        vtyOut = findViewById(R.id.vty_out)
        trunkState = findViewById(R.id.trunk_state)
        modeState = findViewById(R.id.mode_state)
        account = findViewById(R.id.account)
        radioRat = findViewById(R.id.radio_rat)
        radioMetric = findViewById(R.id.radio_metric)
        radioCells = findViewById(R.id.radio_cells)

        findViewById<Button>(R.id.btn_join).setOnClickListener { enrol() }
        findViewById<Button>(R.id.btn_direct).setOnClickListener { direct() }
        findViewById<Button>(R.id.btn_call).setOnClickListener { call() }
        btnAnswer.setOnClickListener { (core.currentCall ?: core.calls.firstOrNull())?.accept() }
        btnHangup.setOnClickListener { core.calls.forEach { it.terminate() } }
        findViewById<Button>(R.id.msg_send).setOnClickListener { sendSms() }
        findViewById<Button>(R.id.msg_wipe).setOnClickListener { wipe() }
        findViewById<Button>(R.id.btn_wipe).setOnClickListener { wipe() }
        findViewById<Button>(R.id.btn_2g).setOnClickListener { force2g(true) }
        findViewById<Button>(R.id.btn_auto).setOnClickListener { force2g(false) }
        findViewById<Button>(R.id.sub_cell).setOnClickListener { radioPage = "cell"; show("radio") }
        findViewById<Button>(R.id.sub_frames).setOnClickListener { radioPage = "frames"; show("radio") }
        findViewById<Button>(R.id.sub_log).setOnClickListener { radioPage = "log"; show("radio") }
        findViewById<Button>(R.id.node_prev).setOnClickListener { nodeStep(-1) }
        findViewById<Button>(R.id.node_next).setOnClickListener { nodeStep(1) }
        findViewById<Button>(R.id.tab_messages).setOnClickListener { show("messages") }
        listOf(R.id.btn_theme0, R.id.btn_theme1, R.id.btn_theme2).forEachIndexed { i, id ->
            findViewById<Button>(id).setOnClickListener {
                if (prefs.getInt("theme", 0) != i) { prefs.edit().putInt("theme", i).apply(); recreate() }
            }
        }
        themeState.text = "Thème : ${listOf("Clair", "Sombre", "Comme le système")[th]}"
        // the frames: filter, pause, clear, and the families in one touch
        findViewById<EditText>(R.id.frames_filter).addTextChangedListener(object : TextWatcher {
            override fun afterTextChanged(e: Editable?) { frames.query = e?.toString() ?: ""; framesNote() }
            override fun beforeTextChanged(c: CharSequence?, a: Int, b: Int, d: Int) {}
            override fun onTextChanged(c: CharSequence?, a: Int, b: Int, d: Int) {}
        })
        findViewById<Button>(R.id.frames_pause).setOnClickListener {
            framesPaused = !framesPaused
            (it as Button).text = if (framesPaused) "▶" else "⏸"
            if (!framesPaused) { framesPending.forEach { f -> frames.add(f) }; framesPending.clear() }
            framesNote()
        }
        findViewById<Button>(R.id.frames_clear).setOnClickListener { frames.clear(); framesPending.clear(); framesNote() }
        val cats = findViewById<LinearLayout>(R.id.frames_cats)
        for ((key, label) in CATEGORIES) {
            val b = Button(this, null, android.R.attr.borderlessButtonStyle).apply {
                text = label; isAllCaps = false; textSize = 13f
                setOnClickListener { frames.category = key; catButtons.forEach { (k, v) -> v.alpha = if (k == key) 1f else 0.5f }; framesNote() }
                alpha = if (key == "all") 1f else 0.5f
            }
            catButtons[key] = b
            cats.addView(b)
        }
        findViewById<Button>(R.id.btn_leave).setOnClickListener { leave() }
        findViewById<Button>(R.id.btn_node).setOnClickListener { setNode(node.text.toString()) }
        findViewById<Button>(R.id.btn_vty).setOnClickListener { runVty() }
        findViewById<Button>(R.id.btn_zadarma).setOnClickListener { trunk("zadarma") }
        findViewById<Button>(R.id.btn_dongle).setOnClickListener { trunk("dongle") }
        listOf(R.id.btn_mode0, R.id.btn_mode1, R.id.btn_mode2).forEachIndexed { i, id ->
            findViewById<Button>(id).setOnClickListener { prefs.edit().putInt("mode", i).apply(); applyMode() }
        }
        findViewById<Button>(R.id.tab_phone).setOnClickListener { show("phone") }
        findViewById<Button>(R.id.tab_radio).setOnClickListener { show("radio") }
        findViewById<Button>(R.id.tab_node).setOnClickListener { show("node") }
        findViewById<Button>(R.id.tab_account).setOnClickListener { show("account") }
        spectrum.onSwipe = { dir -> nodeStep(-dir) }          // swipe left: the next pane

        core = Factory.instance().createCore(null, null, this)
        core.addListener(listener)
        core.start()

        val wanted = arrayOf(
            Manifest.permission.RECORD_AUDIO, Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.READ_PHONE_STATE
        ).filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), 1)

        node.setText(host)
        if (enrolled) register()
        applyMode()
        show(if (mode == 0 || enrolled) "phone" else "radio")
        if (host.isNotEmpty()) trunk(null)
        netNote()
    }

    override fun onResume() {
        super.onResume()
        ui.post(poll)
    }

    override fun onPause() {
        ui.removeCallbacks(poll)
        super.onPause()
    }

    override fun onDestroy() {
        frameSource?.stop()
        core.removeListener(listener)
        core.stop()
        super.onDestroy()
    }

    // ── Plumbing ─────────────────────────────────────────────────────────────
    private fun log(line: String) {
        val t = SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date())
        journal.text = "$t  $line\n${journal.text}"
        journalRadio.text = journal.text
    }

    /** The call card takes the place of the number field: who, and where it stands. */
    private fun showCall(peer: String?, info: String) {
        val busy = peer != null
        callCard.visibility = if (busy) View.VISIBLE else View.GONE
        number.visibility = if (busy) View.GONE else View.VISIBLE
        findViewById<View>(R.id.btn_call).visibility = if (busy) View.GONE else View.VISIBLE
        if (busy) { callPeer.text = peer; callInfo.text = info } else callSince = 0L
    }

    /** Calls and conversations, erased: bubbles, journals, the typed number. */
    private fun wipe() {
        msgList.removeAllViews()
        msgTo.setText(""); msgText.setText("")
        journal.text = ""; journalRadio.text = ""
        if (core.callsNb == 0) number.setText("")
        status.text = "Appels et conversations effacés"
        ui.postDelayed({ showStatus() }, 1500)
    }

    private fun dp(v: Float) = (v * resources.displayMetrics.density).toInt()

    private fun bubble(peer: String, text: String, out: Boolean) {
        val t = SimpleDateFormat("HH:mm", Locale.ROOT).format(Date())
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = if (out) Gravity.END else Gravity.START }
        val body = TextView(this).apply {
            this.text = text; textSize = 16f; setTextIsSelectable(true)
            setPadding(dp(14f), dp(9f), dp(14f), dp(9f)); maxWidth = dp(280f)
            setTextColor(if (out) Color.WHITE else if (dark) Color.WHITE else Color.rgb(36, 31, 49))
            background = GradientDrawable().apply {
                cornerRadius = dp(18f).toFloat()
                setColor(if (out) Color.rgb(38, 162, 105) else if (dark) Color.rgb(54, 54, 60) else Color.rgb(228, 228, 232))
            }
        }
        val cap = TextView(this).apply { this.text = "${if (out) "à" else "de"} $peer · $t"; textSize = 11f; alpha = 0.6f }
        val wrap = { LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT) }
        box.addView(body, wrap()); box.addView(cap, wrap())
        msgList.addView(box, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(8f) })
        msgScroll.post { msgScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun showStatus() {
        status.text = if (enrolled) "${prefs.getString("msisdn", "")} · $online" else "Network Monitor"
        account.text = if (enrolled) "Numéro : ${prefs.getString("msisdn", "")}\n" +
            "Compte SIP : ${prefs.getString("sip_user", "")}@${prefs.getString("sip_domain", "")}:${prefs.getInt("sip_port", 5060)}\n" +
            "État : $online" else "Pas de compte sur un nœud."
    }

    private fun http(url: String, body: JSONObject? = null, timeout: Int = 4000): JSONObject {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = timeout
        c.readTimeout = timeout
        if (body != null) {
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray()) }
        }
        val stream = if (c.responseCode in 200..299) c.inputStream else c.errorStream
        return JSONObject(stream?.bufferedReader()?.readText() ?: "{}")
    }

    /** Network off the main thread; the result comes back on it. */
    private fun bg(work: () -> JSONObject, done: (JSONObject?, String?) -> Unit) {
        thread {
            val r: Pair<JSONObject?, String?> = try {
                Pair(work(), null)
            } catch (e: Exception) {
                Pair(null, e.message ?: e.javaClass.simpleName)
            }
            runOnUiThread { if (!isDestroyed) done(r.first, r.second) }
        }
    }

    private fun why(d: JSONObject?, err: String?): String =
        d?.optString("err")?.takeIf { it.isNotEmpty() } ?: d?.optString("error")?.takeIf { it.isNotEmpty() }
            ?: err ?: "le nœud ne répond pas"

    // ── Tabs and modes ───────────────────────────────────────────────────────
    private fun vis(id: Int, on: Boolean) { findViewById<View>(id).visibility = if (on) View.VISIBLE else View.GONE }

    private fun show(name: String) {
        tab = name
        val framesOn = name == "radio" && radioPage == "frames"
        vis(R.id.scroll, name != "messages" && !framesOn)
        vis(R.id.panel_frames, framesOn)
        vis(R.id.panel_messages, name == "messages")
        vis(R.id.panel_join, name == "phone" && !enrolled)
        vis(R.id.panel_phone, name == "phone" && enrolled)
        vis(R.id.panel_radio, name == "radio" && radioPage == "cell")
        vis(R.id.panel_log, name == "radio" && radioPage == "log")
        vis(R.id.panel_node, name == "node")
        vis(R.id.panel_account, name == "account")
        vis(R.id.nav_radio, name == "radio")
        vis(R.id.nav_node, name == "node")
        listOf("cell" to R.id.sub_cell, "frames" to R.id.sub_frames, "log" to R.id.sub_log).forEach { (k, id) ->
            findViewById<View>(id).alpha = if (k == radioPage) 1f else 0.5f
        }
        showStatus()
        etatAt = 0
        cellsAt = 0
        if (name == "node") showNode()
        if (name == "radio") ensureFrames()
    }

    /** The node, one pane at a time: BTS, MS, services, console (Expert). */
    private fun showNode() {
        val pages = nodePages
        nodePage = nodePage.coerceIn(0, pages.size - 1)
        val p = pages[nodePage]
        vis(R.id.spectrum, p == "bts" || p == "ms")
        vis(R.id.services, p == "services")
        vis(R.id.panel_console, p == "console")
        nodeTitle.text = "${NODE_TITLES[p]}\n${pages.indices.joinToString(" ") { if (it == nodePage) "●" else "○" }}"
        if (host.isEmpty()) spectrum.fail("pas de nœud : son adresse dans Compte")
        etatAt = 0
    }

    private fun nodeStep(d: Int) {
        nodePage = (nodePage + d).coerceIn(0, nodePages.size - 1)
        showNode()
    }

    /** The frames follow the node: one source, restarted when the address changes. */
    private fun ensureFrames() {
        val h = host
        if (h == framesHost && frameSource != null) return
        frameSource?.stop(); frameSource = null; framesHost = h
        if (h.isEmpty()) { framesNote = "pas de nœud : son adresse dans Compte"; framesNote(); return }
        frameSource = FrameSource("ws://$h:8080/", { f ->
            if (framesPaused) { framesPending.add(f); if (framesPending.size > 2000) framesPending.removeAt(0) } else frames.add(f)
            if (framesNote.endsWith("capture demandée")) framesNote = framesNote.removeSuffix("demandée") + "active"
            framesNote()
        }, { t, _ -> framesNote = t; framesNote() })
    }

    private fun framesNote() {
        framesStatus.text = "$framesNote · ${frames.count} / ${frames.total()} trame(s)" +
            if (framesPaused) " · en pause (${framesPending.size})" else ""
    }

    private fun applyMode() {
        val m = mode
        modeState.text = "Mode : ${listOf("Basique", "Medium", "Expert")[m]}"
        vis(R.id.tab_radio, m >= 1)
        vis(R.id.tab_node, m >= 1)
        vis(R.id.panel_trunk, m == 2)
        if (m == 0 && (tab == "radio" || tab == "node")) show("phone") else show(tab)
    }

    private fun tick() {
        val now = SystemClock.elapsedRealtime()
        if (callSince != 0L && callCard.visibility == View.VISIBLE) {
            val sec = (now - callSince) / 1000
            callInfo.text = "En communication · %02d:%02d".format(sec / 60, sec % 60)
        }
        if (tab == "radio" && radioPage == "cell" && now - cellsAt > 2000) {
            cellsAt = now
            refreshCells()
        }
        if (tab != "node" || host.isEmpty()) return
        val h = host
        val page = nodePages[nodePage.coerceIn(0, nodePages.size - 1)]
        if ((page == "bts" || page == "ms") && !psdBusy) {
            psdBusy = true
            bg({ http("http://$h:8081/psd?src=$page", null, 3000) }) { d, err ->
                psdBusy = false
                val p = d?.optJSONArray("psd")
                if (d == null || p == null || p.length() == 0) {
                    spectrum.fail(if (d == null) "nœud injoignable ($err)" else why(d, err))
                } else {
                    val f = d.optJSONArray("freqs")
                    spectrum.push(
                        DoubleArray(p.length()) { p.optDouble(it, -120.0) },
                        f?.optDouble(0, -541.0) ?: -541.0,
                        f?.optDouble(f.length() - 1, 541.0) ?: 541.0,
                        "${d.optString("label")} · ARFCN ${d.opt("arfcn") ?: "?"}"
                    )
                }
            }
        }
        if ((page == "services" || page == "console") && now - etatAt > 5000) {
            etatAt = now
            bg({ http("http://$h:8090/api/etat") }) { d, err -> showEtat(d, err) }
        }
    }

    // ── The node ─────────────────────────────────────────────────────────────
    private fun setNode(typed: String) {
        val h = typed.trim().replace(Regex("^[a-z]+://"), "").substringBefore('/').substringBefore(':')
        prefs.edit().putString("node", h).apply()
        node.setText(h)
        etatAt = 0
        services.text = ""
        if (h.isNotEmpty()) trunk(null) else trunkState.text = "Sortie vers l'extérieur : —"
        if (tab == "radio") ensureFrames()
    }

    private fun showEtat(d: JSONObject?, err: String?) {
        val procs = d?.optJSONArray("procs")
        if (procs == null) {
            services.text = "Services indisponibles : osmo-reglages ne répond pas sur le port 8090 (${why(d, err)})"
            return
        }
        val sb = StringBuilder("Services du nœud (${procs.length()})\n\n")
        for (i in 0 until procs.length()) {
            val p = procs.getJSONObject(i)
            val name = p.optString("cmd").substringBefore(' ').substringAfterLast('/')
            sb.append("● %-16s %5s %%  %4d Mo  %d min\n".format(name, p.optString("cpu"), p.optInt("rss"), p.optInt("age") / 60))
        }
        services.text = sb.toString()
        val v = d.optJSONArray("vty") ?: JSONArray()
        if (v.toString() != vty.toString()) {
            vty = v
            val names = (0 until v.length()).map {
                val o = v.getJSONObject(it)
                "${o.optString("invite", o.optString("nom"))}  (${o.optString("hote")}:${o.optInt("port")})"
            }
            vtySel.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, names)
        }
    }

    private fun runVty() {
        val cmd = vtyCmd.text.toString().trim()
        val i = vtySel.selectedItemPosition
        if (cmd.isEmpty() || host.isEmpty() || i < 0 || i >= vty.length()) {
            vtyOut.text = "Il faut un nœud, un démon VTY (liste au-dessus) et une commande.\n${vtyOut.text}"
            return
        }
        val o = vty.getJSONObject(i)
        val body = JSONObject().put("hote", o.optString("hote")).put("port", o.optInt("port"))
            .put("lignes", JSONArray().put(cmd))
        val h = host
        vtyCmd.setText("")
        bg({ http("http://$h:8090/api/console", body, 10000) }) { d, err ->
            val out = d?.optString("sortie")?.takeIf { it.isNotEmpty() } ?: "> $cmd\n${why(d, err)}"
            vtyOut.text = "$out\n\n${vtyOut.text}"
        }
    }

    /** The outbound trunk of the node: read (null) or chosen. */
    private fun trunk(choice: String?) {
        if (host.isEmpty()) {
            trunkState.text = "Sortie vers l'extérieur : pas de nœud"
            return
        }
        val h = host
        val body = if (choice == null) null else JSONObject().put("trunk", choice)
        bg({ http("http://$h:8090/api/trunk", body) }) { d, err ->
            val t = d?.optString("trunk")?.takeIf { it.isNotEmpty() }
            trunkState.text = "Sortie vers l'extérieur : ${t ?: "indisponible (${why(d, err)})"}"
        }
    }

    // ── The radio seen by Android ────────────────────────────────────────────
    private fun v(x: Int) = if (x == Int.MAX_VALUE) "—" else x.toString()

    @SuppressLint("MissingPermission")
    private fun refreshCells() {
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            radioCells.text = "Android ne donne les cellules qu'avec l'autorisation de localisation."
            return
        }
        val tm = getSystemService(TELEPHONY_SERVICE) as TelephonyManager
        val cells = try {
            tm.allCellInfo ?: emptyList()
        } catch (e: SecurityException) {
            emptyList()
        }
        var head = "—"
        var metric = ""
        val sb = StringBuilder()
        for (c in cells) {
            val reg = c.isRegistered
            val line = when (c) {
                is CellInfoLte -> {
                    val i = c.cellIdentity
                    val s = c.cellSignalStrength
                    if (reg) { head = "4G LTE  ${tm.networkOperatorName}"; metric = "${v(s.rsrp)} dBm" }
                    "LTE   PLMN ${i.mccString ?: "?"}-${i.mncString ?: "?"}  TAC ${v(i.tac)}  CI ${v(i.ci)}\n" +
                        "      PCI ${v(i.pci)}  EARFCN ${v(i.earfcn)}\n" +
                        "      RSRP ${v(s.rsrp)}  RSRQ ${v(s.rsrq)}  SNR ${v(s.rssnr)}  RSSI ${v(s.rssi)}"
                }
                is CellInfoNr -> {
                    val i = c.cellIdentity as CellIdentityNr
                    val s = c.cellSignalStrength as CellSignalStrengthNr
                    if (reg) { head = "5G NR  ${tm.networkOperatorName}"; metric = "${v(s.ssRsrp)} dBm" }
                    "NR    PLMN ${i.mccString ?: "?"}-${i.mncString ?: "?"}  TAC ${v(i.tac)}  NCI ${i.nci}\n" +
                        "      PCI ${v(i.pci)}  NR-ARFCN ${v(i.nrarfcn)}\n" +
                        "      SS-RSRP ${v(s.ssRsrp)}  SS-RSRQ ${v(s.ssRsrq)}  SS-SINR ${v(s.ssSinr)}"
                }
                is CellInfoWcdma -> {
                    val i = c.cellIdentity
                    val s = c.cellSignalStrength
                    if (reg) { head = "3G UMTS  ${tm.networkOperatorName}"; metric = "${v(s.dbm)} dBm" }
                    "UMTS  PLMN ${i.mccString ?: "?"}-${i.mncString ?: "?"}  LAC ${v(i.lac)}  CID ${v(i.cid)}\n" +
                        "      PSC ${v(i.psc)}  UARFCN ${v(i.uarfcn)}  RSCP ${v(s.dbm)}"
                }
                is CellInfoGsm -> {
                    val i = c.cellIdentity
                    val s = c.cellSignalStrength
                    if (reg) { head = "2G GSM  ${tm.networkOperatorName}"; metric = "${v(s.dbm)} dBm" }
                    "GSM   PLMN ${i.mccString ?: "?"}-${i.mncString ?: "?"}  LAC ${v(i.lac)}  CID ${v(i.cid)}\n" +
                        "      ARFCN ${v(i.arfcn)}  BSIC ${v(i.bsic)}  RSSI ${v(s.dbm)}"
                }
                else -> c.javaClass.simpleName
            }
            sb.append(if (reg) "● " else "○ ").append(line).append("\n\n")
        }
        radioRat.text = head
        radioMetric.text = metric
        radioCells.text = if (cells.isEmpty()) "Aucune cellule donnée par le modem." else sb.toString()
    }

    // ── Enrolment ────────────────────────────────────────────────────────────
    private fun enrol() {
        val pseudo = user.text.toString().trim()
        val code = secret.text.toString().trim()
        val h = host
        if (h.isEmpty() || pseudo.isEmpty() || code.isEmpty()) {
            status.text = "Il faut l'adresse du nœud (Compte), un pseudo et le code"
            return
        }
        status.text = "Inscription…"
        bg({ http("http://$h:8088/inscription", JSONObject().put("code", code).put("pseudo", pseudo), 10000) }) { j, err ->
            if (j == null || !j.optBoolean("ok") || j.optString("sip_user").isEmpty()) {
                status.text = "Inscription refusée : ${why(j, err)}"
            } else {
                prefs.edit()
                    .putString("msisdn", j.optString("msisdn", j.optString("sip_user")))
                    .putString("sip_user", j.optString("sip_user"))
                    .putString("sip_password", j.optString("sip_password"))
                    .putString("sip_domain", j.optString("sip_domain", h))
                    .putInt("sip_port", j.optInt("sip_port", 5060))
                    .putString("sip_transport", j.optString("sip_transport", "udp"))
                    .apply()
                register()
                show("phone")
            }
        }
    }

    private fun direct() {
        val u = user.text.toString().trim()
        if (host.isEmpty() || u.isEmpty()) {
            status.text = "Il faut l'adresse du nœud (Compte) et l'identifiant SIP"
            return
        }
        prefs.edit()
            .putString("msisdn", u)
            .putString("sip_user", u)
            .putString("sip_password", secret.text.toString())
            .putString("sip_domain", host)
            .putInt("sip_port", 5060)
            .putString("sip_transport", "udp")
            .apply()
        register()
        show("phone")
    }

    private fun register() {
        val u = prefs.getString("sip_user", "") ?: ""
        val domain = prefs.getString("sip_domain", "") ?: ""
        val f = Factory.instance()
        val identity = f.createAddress("sip:$u@$domain")
        val server = f.createAddress("sip:$domain:${prefs.getInt("sip_port", 5060)}")
        if (identity == null || server == null) {
            log("Compte illisible : $u@$domain")
            return
        }
        server.transport = when (prefs.getString("sip_transport", "udp")) {
            "tls" -> TransportType.Tls
            "tcp" -> TransportType.Tcp
            else -> TransportType.Udp
        }
        core.clearAccounts()
        core.clearAllAuthInfo()
        val params = core.createAccountParams()
        params.identityAddress = identity
        params.serverAddress = server
        params.isRegisterEnabled = true
        core.addAuthInfo(f.createAuthInfo(u, null, prefs.getString("sip_password", ""), null, null, domain, null))
        val acc = core.createAccount(params)
        core.addAccount(acc)
        core.defaultAccount = acc
    }

    private fun leave() {
        core.calls.forEach { it.terminate() }
        core.clearAccounts()
        core.clearAllAuthInfo()
        val keepNode = host
        val keepMode = mode
        val keepTheme = prefs.getInt("theme", 0)
        prefs.edit().clear().putString("node", keepNode).putInt("mode", keepMode).putInt("theme", keepTheme).apply()
        online = "Hors ligne"
        show("phone")
    }

    // ── Calls and SMS ────────────────────────────────────────────────────────
    private fun peer() = Factory.instance().createAddress(
        "sip:${number.text.toString().trim().replace(" ", "")}@${prefs.getString("sip_domain", "")}"
    )

    private fun call() {
        val to = peer()
        if (number.text.isBlank() || to == null) {
            log("Numéro manquant")
            return
        }
        val params = core.createCallParams(null)
        params?.mediaEncryption = MediaEncryption.None
        if (params == null) core.inviteAddress(to) else core.inviteAddressWithParams(to, params)
    }

    private fun sendSms() {
        val num = msgTo.text.toString().trim().replace(" ", "")
        val body = msgText.text.toString()
        val to = Factory.instance().createAddress("sip:$num@${prefs.getString("sip_domain", "")}")
        if (!enrolled) { status.text = "Pas de compte sur un nœud (onglet Tél.)"; return }
        if (num.isEmpty() || to == null || body.isEmpty()) {
            status.text = "Numéro ou message manquant"
            return
        }
        // SIP MESSAGE, one basic chat room per peer.
        val room = core.getChatRoom(to)
        if (room == null) {
            log("SMS impossible vers $num")
            return
        }
        room.createMessageFromUtf8(body).send()
        log("SMS à $num : $body")
        bubble(num, body, true)
        msgText.setText("")
    }

    // ── Forcing 2G ───────────────────────────────────────────────────────────
    // An ordinary app cannot choose the network type: Android asks for
    // MODIFY_PHONE_STATE (system apps) or CARRIER PRIVILEGES, which a SIM gives
    // to the apps whose signing certificate is in its access rules (ARA-M /
    // ARF, written with pySim on a bench SIM). With them, GSM only and back;
    // without, the hidden "phone info" screen (*#*#4636#*#*), where the
    // preferred network type is chosen by hand.
    private val GSM_ONLY = TelephonyManager.NETWORK_TYPE_BITMASK_GSM or
        TelephonyManager.NETWORK_TYPE_BITMASK_GPRS or TelephonyManager.NETWORK_TYPE_BITMASK_EDGE
    private val REASON_CARRIER = 2          // TelephonyManager.ALLOWED_NETWORK_TYPES_REASON_CARRIER

    private fun certHash(): String = try {
        @Suppress("DEPRECATION")
        val info = packageManager.getPackageInfo(packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        val sig = info.signingInfo?.apkContentsSigners?.firstOrNull()
        if (sig == null) "?" else MessageDigest.getInstance("SHA-256").digest(sig.toByteArray())
            .joinToString("") { "%02X".format(it) }
    } catch (e: Exception) { "?" }

    private fun privileged(): Boolean = try {
        (getSystemService(TELEPHONY_SERVICE) as TelephonyManager).hasCarrierPrivileges()
    } catch (e: Exception) { false }

    private fun netNote(extra: String = "") {
        netState.text = (if (extra.isEmpty()) "" else "$extra\n") +
            (if (privileged()) "Privilèges opérateur : oui, l'appli peut forcer la 2G."
            else "Privilèges opérateur : non. « Forcer la 2G » ouvre l'écran caché du téléphone " +
                "(Type de réseau préféré → GSM only). Pour un vrai forçage : règle ARA-M de la SIM, " +
                "certificat SHA-256 ${certHash()}")
    }

    @SuppressLint("MissingPermission", "WrongConstant", "NewApi")
    private fun force2g(on: Boolean) {
        val tm = getSystemService(TELEPHONY_SERVICE) as TelephonyManager
        if (privileged() && Build.VERSION.SDK_INT >= 31) {
            try {
                if (on) {
                    if (!prefs.contains("net_before"))
                        prefs.edit().putLong("net_before", tm.getAllowedNetworkTypesForReason(REASON_CARRIER)).apply()
                    tm.setAllowedNetworkTypesForReason(REASON_CARRIER, GSM_ONLY)
                    log("Réseau : 2G forcée (GSM seul)")
                    netNote("2G forcée : le téléphone ne campe plus que sur le GSM.")
                } else if (prefs.contains("net_before")) {
                    tm.setAllowedNetworkTypesForReason(REASON_CARRIER, prefs.getLong("net_before", GSM_ONLY))
                    prefs.edit().remove("net_before").apply()
                    log("Réseau : retour au choix automatique")
                    netNote("Réseau automatique rétabli.")
                } else netNote("Rien à rétablir : la 2G n'avait pas été forcée par l'appli.")
                return
            } catch (e: Exception) {
                netNote("Forçage refusé par Android : ${e.message ?: e.javaClass.simpleName}")
            }
        }
        // No privileges: the hidden radio-info screen, then the network settings.
        val tries = listOf(
            Intent(Intent.ACTION_MAIN).setClassName("com.android.phone", "com.android.phone.settings.RadioInfo"),
            Intent(Intent.ACTION_MAIN).setClassName("com.android.settings", "com.android.settings.RadioInfo"),
            Intent(Settings.ACTION_NETWORK_OPERATOR_SETTINGS),
            Intent(Settings.ACTION_WIRELESS_SETTINGS)
        )
        for (i in tries) {
            try {
                startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                netNote(if (on) "Dans l'écran ouvert : Type de réseau préféré → GSM only."
                        else "Dans l'écran ouvert : remettre le type de réseau préféré d'origine.")
                return
            } catch (e: Exception) { /* the next one */ }
        }
        netNote("Aucun écran de réglage réseau accessible sur ce téléphone.")
    }
}
