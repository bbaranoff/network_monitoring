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
import android.net.Uri
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import android.graphics.BitmapFactory
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
import android.telecom.TelecomManager
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
import org.linphone.core.AudioDevice
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
    private var appDialing = false
    private val appCalls = HashSet<Call>()
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
    private var speakerOn = false
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
            // A call dialled FROM THIS APP stays in this app, proxy or not: it is
            // not in the native Phone app, so its Hang up button must be here.
            if (appDialing && state == Call.State.OutgoingInit) appCalls.add(call)
            val own = appCalls.contains(call)
            if (state == Call.State.Released) appCalls.remove(call)
            if (Bench.nativeActive(this@MainActivity) && !own) return     // the native Phone app has the call
            val peer = call.remoteAddress.username ?: call.remoteAddress.asStringUriOnly()
            // An incoming call must be HEARD, app in the background included.
            if (state == Call.State.IncomingReceived) Bench.ring(this@MainActivity, peer) else Bench.stopRing()
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
            if (Bench.native(this@MainActivity)) {                // proxy on: a system notification
                Bench.notify(this@MainActivity, "SMS de $from", body)
                return
            }
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
        // ... and "Télécom" (3): dark, in the colours of the bench poster.
        dark = th == 1 || th == 3 || (th == 2 && night)
        setTheme(if (th == 3) R.style.Telco else if (dark) android.R.style.Theme_Material_NoActionBar
            else android.R.style.Theme_Material_Light_NoActionBar)
        super.onCreate(savedInstanceState)
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        setContentView(R.layout.main)
        // The background: the illustrations of the osmo-operator desktop (the
        // city, the mast, the landscape), cropped and veiled so the text reads.
        window.setBackgroundDrawable(CoverDrawable(BitmapFactory.decodeResource(resources,
            if (th == 3) R.drawable.fond_telecom else if (dark) R.drawable.fond_sombre else R.drawable.fond_clair)))

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
        findViewById<Button>(R.id.btn_scan).setOnClickListener { scanQr() }
        findViewById<Button>(R.id.btn_direct).setOnClickListener { direct() }
        findViewById<Button>(R.id.btn_call).setOnClickListener { call() }
        btnAnswer.setOnClickListener { (core.currentCall ?: core.calls.firstOrNull())?.accept() }
        btnHangup.setOnClickListener { core.calls.forEach { it.terminate() } }
        findViewById<Button>(R.id.btn_mute).setOnClickListener {
            core.isMicEnabled = !core.isMicEnabled
            (it as Button).text = if (core.isMicEnabled) "Micro" else "Micro coupé"
        }
        findViewById<Button>(R.id.btn_spk).setOnClickListener { speaker(!speakerOn); (it as Button).text = if (speakerOn) "Haut-parleur ✓" else "Haut-parleur" }
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
        findViewById<Button>(R.id.tab_proxy).setOnClickListener { show("proxy") }
        findViewById<EditText>(R.id.proxy_dest).setText(prefs.getString("proxy_dest", "100101"))
        findViewById<Button>(R.id.btn_natif_on).setOnClickListener { natif(true) }
        findViewById<Button>(R.id.btn_redirect).setOnClickListener { askRedirection(true) }
        findViewById<Button>(R.id.btn_natif_off).setOnClickListener { natif(false) }
        findViewById<Button>(R.id.btn_natif_comptes).setOnClickListener { openAccounts() }
        // the quick start: every setting of the first run, in order
        findViewById<Button>(R.id.btn_quick).setOnClickListener { show("quick") }
        findViewById<Button>(R.id.q_scan).setOnClickListener { quickFrom = true; scanQr() }
        findViewById<Button>(R.id.q_perms).setOnClickListener {
            val missing = ALL_PERMS.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
            if (missing.isEmpty()) quickNote() else requestPermissions(missing.toTypedArray(), 6)
        }
        findViewById<Button>(R.id.q_natif).setOnClickListener { natif(true); quickNote() }
        findViewById<Button>(R.id.q_sim).setOnClickListener { proxy(true); ui.postDelayed({ quickNote() }, 2500) }
        findViewById<Button>(R.id.q_rcs).setOnClickListener {
            try { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) } catch (e: Exception) { }
        }
        findViewById<Button>(R.id.q_bt).setOnClickListener { btPair(); ui.postDelayed({ quickNote() }, 4000) }
        findViewById<Button>(R.id.q_done).setOnClickListener {
            prefs.edit().putBoolean("quick_done", true).apply()
            show(if (enrolled) "phone" else "radio")
        }
        findViewById<Button>(R.id.btn_bt_pair).setOnClickListener { btPair() }
        findViewById<Button>(R.id.btn_proxy_rcs).setOnClickListener {
            try { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
            catch (e: Exception) { proxyNote("Paramètres > Notifications > Accès aux notifications : autorisez Network Monitor.") }
        }
        findViewById<Button>(R.id.btn_proxy_on).setOnClickListener { proxy(true) }
        findViewById<Button>(R.id.btn_proxy_off).setOnClickListener { proxy(false) }
        // With an account, the line is held with the app closed: a foreground
        // service keeps the process (and the SIP stack) alive, so a call rings.
        if (enrolled) startForegroundService(Intent(this, ProxyService::class.java))
        listOf(R.id.btn_theme0, R.id.btn_theme1, R.id.btn_theme2, R.id.btn_theme3).forEachIndexed { i, id ->
            findViewById<Button>(id).setOnClickListener {
                if (prefs.getInt("theme", 0) != i) { prefs.edit().putInt("theme", i).apply(); recreate() }
            }
        }
        themeState.text = "Thème : ${listOf("Clair", "Sombre", "Comme le système", "Télécom").getOrElse(th) { "Clair" }}"
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

        // The SIP stack belongs to the process, not to this screen: with the
        // proxy on, the native Phone app drives it while the activity is gone.
        core = Bench.core ?: Factory.instance().createCore(null, null, applicationContext).also { it.start() }
        Bench.hook(this, core)
        core.addListener(listener)

        val wanted = arrayOf(
            Manifest.permission.RECORD_AUDIO, Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.READ_PHONE_STATE
        ).plus(if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray())
        .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), 1)

        node.setText(host)
        if (enrolled) register()
        applyMode()
        // first run: the quick start takes the screen
        show(if (!prefs.getBoolean("quick_done", false)) "quick" else if (mode == 0 || enrolled) "phone" else "radio")
        if (host.isNotEmpty()) trunk(null)
        netNote()
    }

    override fun onResume() {
        super.onResume()
        if (tab == "proxy") { natifNote(); proxyNote() }    // back from a settings screen
        if (tab == "quick") quickNote()
        // back from the calling-accounts screen with « Banc » enabled: the role, once
        if (!roleAsked && Bench.nativeActive(this) && !Bench.redirects(this)) askRedirection()
        ui.post(poll)
    }

    override fun onPause() {
        ui.removeCallbacks(poll)
        super.onPause()
    }

    override fun onDestroy() {
        frameSource?.stop()
        core.removeListener(listener)
        if (!enrolled) { core.stop(); Bench.core = null }       // an account: the stack outlives the screen
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
        if (busy) { callPeer.text = peer; callInfo.text = info } else {
            callSince = 0L
            core.isMicEnabled = true                      // the microphone back, the speaker off
            speakerOn = false
            findViewById<Button>(R.id.btn_mute).text = "Micro"
            findViewById<Button>(R.id.btn_spk).text = "Haut-parleur"
        }
    }

    /** The loudspeaker or the earpiece, for the call in progress. */
    private fun speaker(on: Boolean) {
        val want = if (on) AudioDevice.Type.Speaker else AudioDevice.Type.Earpiece
        val dev = core.audioDevices.firstOrNull { it.type == want && it.hasCapability(AudioDevice.Capabilities.CapabilityPlay) }
        if (dev != null) { core.currentCall?.outputAudioDevice = dev; speakerOn = on }
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
        vis(R.id.panel_quick, name == "quick")
        if (name == "quick") quickNote()
        vis(R.id.panel_join, name == "phone" && !enrolled)
        vis(R.id.panel_phone, name == "phone" && enrolled)
        vis(R.id.panel_radio, name == "radio" && radioPage == "cell")
        vis(R.id.panel_log, name == "radio" && radioPage == "log")
        vis(R.id.panel_node, name == "node")
        vis(R.id.panel_account, name == "account")
        vis(R.id.panel_proxy, name == "proxy")
        if (name == "proxy") { proxyNote(); natifNote(); btNote() }
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
        vis(R.id.tab_proxy, m >= 1)
        vis(R.id.panel_trunk, m == 2)
        if (m == 0 && (tab == "radio" || tab == "node" || tab == "proxy")) show("phone") else show(tab)
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
            services.text = "Le service de réglages du nœud (osmo-reglages, port 8090) ne répond pas.\n" +
                "Les services et la console VTY passent par lui."
            findViewById<View>(R.id.btn_vty).isEnabled = false
            return
        }
        val sb = StringBuilder("Services du nœud (${procs.length()})\n\n")
        for (i in 0 until procs.length()) {
            val p = procs.getJSONObject(i)
            val name = p.optString("cmd").substringBefore(' ').substringAfterLast('/')
            sb.append("● %-16s %5s %%  %4d Mo  %d min\n".format(name, p.optString("cpu"), p.optInt("rss"), p.optInt("age") / 60))
        }
        services.text = sb.toString()
        findViewById<View>(R.id.btn_vty).isEnabled = true
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
            trunkState.text = "Sortie vers l'extérieur : ${t ?: "indisponible, le service de réglages du nœud ne répond pas"}"
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

    // ── The quick start ──────────────────────────────────────────────────────
    private var quickFrom = false
    private val ALL_PERMS: Array<String> = arrayOf(
        Manifest.permission.RECORD_AUDIO, Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_PHONE_NUMBERS,
        Manifest.permission.CALL_PHONE,
        Manifest.permission.READ_CALL_LOG, Manifest.permission.RECEIVE_SMS, Manifest.permission.SEND_SMS,
        Manifest.permission.ACCESS_FINE_LOCATION
    ) + (if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray()) +
        (if (Build.VERSION.SDK_INT >= 31) arrayOf(Manifest.permission.BLUETOOTH_CONNECT) else emptyArray())

    /** A tick in front of what is done; what is still missing, in words. */
    private fun quickNote() {
        fun mark(id: Int, done: Boolean) {
            val b = findViewById<Button>(id)
            val t = b.text.toString().removePrefix("✓ ")
            b.text = if (done) "✓ $t" else t
        }
        val perms = ALL_PERMS.all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
        mark(R.id.q_scan, enrolled)
        mark(R.id.q_perms, perms)
        mark(R.id.q_natif, Bench.nativeActive(this))
        mark(R.id.q_sim, Proxy.enabled(this))
        mark(R.id.q_rcs, rcsAllowed())
        val st = findViewById<TextView>(R.id.q_state)
        st.text = when {
            !enrolled -> "Commencez par l'étape 1 : sans compte sur un nœud, le reste ne peut pas se régler."
            !perms -> "Compte : ${prefs.getString("msisdn", "")} sur ${host}. Accordez les autorisations (étape 2)."
            Bench.native(this) && !Bench.accountEnabled(this) ->
                "Étape 3 : activez le compte « Banc » dans l'écran des comptes téléphoniques, puis revenez ici."
            Bench.nativeActive(this) && !Bench.redirects(this) ->
                "Étape 3 : acceptez aussi la redirection des appels, pour que le clavier natif confie les numéros du banc à « Banc »."
            else -> "Compte : ${prefs.getString("msisdn", "")} sur ${host}. Les étapes 3 à 6 sont à choisir selon l'usage ; touchez Terminer quand c'est bon."
        }
        if (enrolled && host.isNotEmpty()) bg({ Proxy.call(this, "/proxy/bluetooth", null) ?: JSONObject() }) { d, _ ->
            mark(R.id.q_bt, d?.optJSONObject("ligne")?.optBoolean("connecte") == true)
        }
    }

    // ── The invitation QR code ───────────────────────────────────────────────
    // osmo-inscription.py --nouveau-qrcode PSEUDO shows a QR code that carries
    //   osmo://inscription?noeud=<address>&pseudo=<pseudo>&code=<16 characters>
    // Reading it is enough: the node, the pseudo and the invitation are in it.
    private fun scanQr() {
        val opts = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        try {
            GmsBarcodeScanning.getClient(this, opts).startScan()
                .addOnSuccessListener { b -> useInvitation(b.rawValue ?: "") }
                .addOnFailureListener { e -> status.text = "Lecteur de QR code indisponible : ${e.message ?: e.javaClass.simpleName}" }
        } catch (e: Exception) {
            status.text = "Lecteur de QR code indisponible : ${e.message ?: e.javaClass.simpleName}"
        }
    }

    private fun useInvitation(text: String) {
        val uri = try { Uri.parse(text.trim()) } catch (e: Exception) { null }
        val noeud = uri?.getQueryParameter("noeud") ?: ""
        val code = uri?.getQueryParameter("code") ?: ""
        if (uri == null || uri.scheme != "osmo" || uri.host != "inscription" || noeud.isEmpty() || code.isEmpty()) {
            status.text = "Ce QR code n'est pas une invitation du nœud"
            return
        }
        setNode(noeud)
        uri.getQueryParameter("pseudo")?.takeIf { it.isNotEmpty() }?.let { user.setText(it) }
        secret.setText(code)
        if (user.text.isNotBlank()) enrol() else status.text = "Invitation lue : indiquez un pseudo, puis « S'inscrire »"
    }

    // ── Enrolment ────────────────────────────────────────────────────────────
    private fun enrol() {
        val pseudo = user.text.toString().trim()
        val code = secret.text.toString().trim()
        val h = host
        if (h.isEmpty() || pseudo.isEmpty() || code.isEmpty()) {
            status.text = "Il faut l'adresse du nœud (Compte), un pseudo et l'invitation - ou scannez le QR code"
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
                startForegroundService(Intent(this, ProxyService::class.java))
                show(if (quickFrom) "quick" else "phone")
                quickFrom = false
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
        if (Proxy.enabled(this)) proxy(false)                    // no account, no proxy
        if (Bench.native(this)) natif(false)
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
        appDialing = true
        try {
            if (params == null) core.inviteAddress(to) else core.inviteAddressWithParams(to, params)
        } finally {
            appDialing = false
        }
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

    // ── The proxy: the bench in the native apps of the phone (Natif.kt) ──────
    private fun natifNote(extra: String = "") {
        val on = Bench.native(this)
        findViewById<TextView>(R.id.natif_state).text = (if (extra.isEmpty()) "" else "$extra\n") + when {
            !on -> "Proxy inactif : appels et SMS du banc s'affichent dans cette appli."
            Bench.accountEnabled(this) -> "Proxy ACTIF : le compte « Banc » est activé dans l'appli Téléphone.\n" +
                (if (Bench.redirects(this)) "Clavier natif : un numéro du banc (3006, 100101, 600…) part tout seul par « Banc »."
                else "Clavier natif : l'appli n'a pas le rôle de redirection des appels ; un numéro du banc partirait " +
                    "par la SIM. Touchez Activer de nouveau et acceptez la demande d'Android.")
            else -> "Proxy demandé, mais le compte « Banc » n'est PAS activé dans l'appli Téléphone : en attendant, " +
                "les appels sonnent dans cette appli. Touchez « Comptes téléphoniques » et activez « Banc » ; " +
                "si ce compte n'y figure pas, ce téléphone ne permet pas les comptes d'appel tiers."
        }
    }

    /** The role that lets the native keypad hand bench numbers to "Banc":
     *  Android asks the user once, in its own dialog. */
    private var roleAsked = false

    private fun askRedirection(say: Boolean = false) {
        try {
            val rm = getSystemService(android.app.role.RoleManager::class.java)
            val role = android.app.role.RoleManager.ROLE_CALL_REDIRECTION
            when {
                rm == null || !rm.isRoleAvailable(role) ->
                    if (say) natifNote("Ce téléphone n'offre pas la redirection des appels : dans le clavier natif, choisissez le compte « Banc » à la main.")
                rm.isRoleHeld(role) -> if (say) natifNote("La redirection est déjà accordée.")
                else -> { roleAsked = true; startActivityForResult(rm.createRequestRoleIntent(role), 5) }
            }
        } catch (e: Exception) {
            if (say) natifNote("Android a refusé la demande : ${e.message ?: e.javaClass.simpleName}. " +
                "Paramètres > Applications > Applications par défaut > Redirection des appels : choisissez Network Monitor.")
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 5) {
            natifNote(if (Bench.redirects(this)) "" else "Redirection non accordée. Paramètres > Applications > Applications par défaut > " +
                "Redirection des appels (ou « Appli de transfert d'appel ») : choisissez Network Monitor.")
            if (tab == "quick") quickNote()
        }
    }

    private fun openAccounts() {
        try { startActivity(Intent(TelecomManager.ACTION_CHANGE_PHONE_ACCOUNTS)) }
        catch (e: Exception) { natifNote("Ouvrez l'appli Téléphone > Paramètres > Appels > Comptes téléphoniques.") }
    }

    /** The permissions were answered: the activation goes on by itself. */
    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        val granted = results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED }
        when (code) {
            2 -> if (granted) proxy(true) else proxyNote("Sans ces autorisations, la SIM ne peut pas être prêtée au banc.")
            3 -> if (granted) natif(true) else natifNote("Sans ces autorisations, les appels du banc ne peuvent pas passer par l'appli Téléphone.")
            6 -> quickNote()
            4 -> if (granted) btPair() else btShow(JSONObject().put("ok", true).put("message", "Sans l'autorisation Bluetooth, l'appli ne peut pas lancer l'association."))
        }
    }

    private fun natif(on: Boolean) {
        if (on) {
            if (!enrolled) return natifNote("Il faut d'abord un compte sur un nœud (onglet Tél.).")
            val missing = (arrayOf(Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_PHONE_NUMBERS,
                Manifest.permission.CALL_PHONE,
                Manifest.permission.RECORD_AUDIO) +
                (if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray()))
                .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
            if (missing.isNotEmpty()) {
                requestPermissions(missing.toTypedArray(), 3)
                return natifNote("Autorisez le micro, le téléphone et les notifications, puis touchez Activer de nouveau.")
            }
            try {
                Bench.register(this)
            } catch (e: Exception) {
                return natifNote("Android a refusé le compte téléphonique : ${e.message ?: e.javaClass.simpleName}")
            }
            prefs.edit().putBoolean("natif", true).apply()
            startForegroundService(Intent(this, ProxyService::class.java))      // keeps the SIP stack alive, app closed
            showCall(null, "")
            log("Proxy : appels et SMS du banc dans les applis du téléphone")
            // The one step Android reserves to the user: the switch of the account.
            // The app opens the screen itself; nothing else to look for.
            // One screen at a time: the account first; the role is asked when the
            // user comes back with the account enabled (onResume).
            if (!Bench.accountEnabled(this)) openAccounts() else askRedirection()
        } else {
            prefs.edit().putBoolean("natif", false).apply()
            Bench.unregister(this)
            log("Proxy désactivé : appels et SMS du banc dans cette appli")
        }
        natifNote()
    }

    // ── Calls over Bluetooth: this phone as a line of the bench ──────────────
    // The node (Asterisk, chan_mobile) plays the hands-free kit; the voice of a
    // call of the SIM then goes to the bench, which no app can do by itself.
    // Everything from here: the node opens a pairing window (it becomes
    // visible and accepts), this phone asks to bond with the node's adapter,
    // Android shows its pairing dialog - the one consent it requires - and the
    // node sets the line up by itself. GET/POST /proxy/bluetooth on the node.
    private var btWatch = 0

    private fun btShow(d: JSONObject?, extra: String = "") {
        val st = findViewById<TextView>(R.id.bt_state)
        if (d == null || !d.optBoolean("ok")) {
            st.text = (if (extra.isEmpty()) "" else "$extra\n") + "Le nœud ne répond pas (osmo-inscription, port 8088)."
            return
        }
        val line = d.optJSONObject("ligne")
        val left = d.optInt("fenetre")
        val node = d.optJSONObject("adaptateur")?.optString("nom") ?: "?"
        val msg = d.optString("message")
        st.text = (if (extra.isEmpty()) "" else "$extra\n") + when {
            left > 0 -> "Le nœud « $node » attend l'association (encore $left s). Acceptez la demande d'association."
            line != null && line.optBoolean("connecte") ->
                "Ligne Bluetooth en place : ${if (line.optString("etat") == "Free") "connectée, libre" else "en appel"}."
            line != null -> "Ligne déclarée, mais ce téléphone est hors de portée du nœud (Bluetooth allumé ?)."
            msg.isNotEmpty() -> msg
            else -> "Aucun téléphone associé au nœud."
        }
    }

    private fun btNote() {
        if (host.isEmpty()) return
        bg({ Proxy.call(this, "/proxy/bluetooth", null) ?: JSONObject() }) { d, _ -> btShow(d) }
    }

    @SuppressLint("MissingPermission")
    private fun btPair() {
        if (!enrolled || host.isEmpty()) return btShow(JSONObject().put("ok", true).put("message", "Il faut d'abord un compte sur un nœud (onglet Tél.)."))
        if (Build.VERSION.SDK_INT >= 31 && checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), 4)
            return
        }
        val adapter = (getSystemService(BLUETOOTH_SERVICE) as android.bluetooth.BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            try { startActivity(Intent(android.bluetooth.BluetoothAdapter.ACTION_REQUEST_ENABLE)) } catch (e: Exception) { }
            return btShow(JSONObject().put("ok", true).put("message", "Allumez le Bluetooth, puis touchez de nouveau « Associer »."))
        }
        bg({ Proxy.call(this, "/proxy/bluetooth/associer", Proxy.who(this)) ?: JSONObject() }) { d, _ ->
            val mac = d?.optJSONObject("adaptateur")?.optString("adresse") ?: ""
            if (d == null || !d.optBoolean("ok") || mac.isEmpty()) {
                btShow(d, "Le nœud n'a pas ouvert l'association${d?.optString("error")?.let { if (it.isEmpty()) "" else " : $it" } ?: ""}.")
                return@bg
            }
            // the phone asks: Android shows its pairing dialog
            val asked = try { adapter.getRemoteDevice(mac).createBond() } catch (e: Exception) { false }
            btShow(d, if (asked) "" else "Association non lancée par Android : associez « ${d.optJSONObject("adaptateur")?.optString("nom")} » depuis les réglages Bluetooth.")
            // follow the node while its window is open, then once more
            val me = ++btWatch
            val follow = object : Runnable {
                var n = 0
                override fun run() {
                    if (me != btWatch || isDestroyed || tab != "proxy") return
                    bg({ Proxy.call(this@MainActivity, "/proxy/bluetooth", null) ?: JSONObject() }) { s, _ ->
                        btShow(s)
                        val busy = (s?.optInt("fenetre") ?: 0) > 0 || (s?.optString("message") ?: "").contains("mise en place")
                        if (++n < 80 && (busy || n < 3)) ui.postDelayed(this, 4000)
                    }
                }
            }
            ui.postDelayed(follow, 3000)
        }
    }

    // ── Lending the SIM to the bench (Proxy.kt) ──────────────────────────────
    private val PROXY_PERMS = arrayOf(
        Manifest.permission.RECEIVE_SMS, Manifest.permission.SEND_SMS,
        Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_CALL_LOG
    ) + (if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray())

    private fun proxyNote(extra: String = "") {
        val st = findViewById<TextView>(R.id.proxy_state)
        val on = Proxy.enabled(this)
        st.text = (if (extra.isEmpty()) "" else "$extra\n") +
            (if (on) "Proxy ACTIF : les SMS de la SIM vont à ${prefs.getString("proxy_dest", "")} sur le banc.\n" +
                (if (rcsAllowed()) "Messages RCS : relayés aussi (accès aux notifications accordé)."
                else "Messages RCS (« chat » de Google Messages) : NON relayés. Ce ne sont pas des SMS ; " +
                    "touchez le bouton ci-dessus et autorisez Network Monitor pour les relayer.")
            else "Proxy inactif.")
        if (!on || host.isEmpty()) return
        bg({ Proxy.call(this, "/proxy", null) ?: JSONObject() }) { d, _ ->
            if (d != null && d.has("actif") && Proxy.enabled(this))
                st.text = "${st.text}\nNœud : ${if (d.optBoolean("actif")) "proxy vu (compte ${d.optString("user")})" else "pas encore vu"} · ${d.optInt("attente")} SMS en attente"
        }
    }

    /** The user gave this app the notification access (needed for RCS). */
    private fun rcsAllowed(): Boolean =
        (Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: "").contains(packageName)

    private fun proxy(on: Boolean) {
        if (on) {
            if (!enrolled || host.isEmpty()) return proxyNote("Il faut d'abord un compte sur un nœud (onglet Tél.).")
            val missing = PROXY_PERMS.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
            if (missing.isNotEmpty()) {
                requestPermissions(missing.toTypedArray(), 2)
                return proxyNote("Autorisez les SMS et le téléphone, puis touchez Activer de nouveau.")
            }
            val dest = findViewById<EditText>(R.id.proxy_dest).text.toString().trim().replace(" ", "")
            if (dest.isEmpty()) return proxyNote("Indiquez le numéro du banc qui reçoit les SMS.")
            prefs.edit().putString("proxy_dest", dest).putBoolean("proxy", true).apply()
            bg({ Proxy.announce(this, true) ?: JSONObject() }) { d, _ ->
                if (d == null || !d.optBoolean("ok")) {
                    prefs.edit().putBoolean("proxy", false).apply()
                    proxyNote("Le nœud a refusé : ${d?.optString("error")?.ifEmpty { "il ne répond pas (osmo-inscription, port 8088)" }}")
                } else {
                    startForegroundService(Intent(this, ProxyService::class.java))
                    log("Proxy activé vers $dest")
                    proxyNote()
                }
            }
        } else {
            prefs.edit().putBoolean("proxy", false).apply()      // the service sees it, withdraws and stops
            bg({ Proxy.announce(this, false) ?: JSONObject() }) { _, _ -> }
            log("Proxy désactivé")
            proxyNote()
        }
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

/** A picture that COVERS its bounds, centred, never stretched (a window
 *  background is otherwise deformed to the size of the screen). */
class CoverDrawable(private val bmp: android.graphics.Bitmap) : android.graphics.drawable.Drawable() {
    private val paint = android.graphics.Paint(android.graphics.Paint.FILTER_BITMAP_FLAG)
    private val m = android.graphics.Matrix()
    override fun draw(canvas: android.graphics.Canvas) {
        val b = bounds
        val k = maxOf(b.width().toFloat() / bmp.width, b.height().toFloat() / bmp.height)
        m.setScale(k, k)
        m.postTranslate(b.left + (b.width() - bmp.width * k) / 2f, b.top + (b.height() - bmp.height * k) / 2f)
        canvas.drawBitmap(bmp, m, paint)
    }
    override fun setAlpha(alpha: Int) { paint.alpha = alpha }
    override fun setColorFilter(cf: android.graphics.ColorFilter?) { paint.colorFilter = cf }
    @Deprecated("Deprecated in Java")
    override fun getOpacity(): Int = android.graphics.PixelFormat.OPAQUE
}
