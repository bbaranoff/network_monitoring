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
// MODES: Basique (phone, account) / Medium (+ radio, spectrum, services) /
// Expert (+ VTY console, trunk choice).
//
// v0.3: the app holds the line only while it is open (no background service);
// the frames page of the postmarketOS app is not here yet.
package org.osmooperator.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.SharedPreferences
import android.content.pm.PackageManager
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
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.Spinner
import android.widget.TextView
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
    private lateinit var text: EditText
    private lateinit var btnAnswer: Button
    private lateinit var btnHangup: Button
    private lateinit var node: EditText
    private lateinit var spectrum: SpectrumView
    private lateinit var spectrumTitle: TextView
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
    private var source = "bts"
    private var psdBusy = false
    private var etatAt = 0L
    private var cellsAt = 0L
    private var vty = JSONArray()
    private var online = "Hors ligne"

    private val host get() = prefs.getString("node", "") ?: ""
    private val mode get() = prefs.getInt("mode", 1)
    private val enrolled get() = prefs.contains("sip_user")

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
                    number.setText(peer)
                    btnAnswer.isEnabled = true
                    btnHangup.isEnabled = true
                    show("phone")
                }
                Call.State.OutgoingProgress -> {
                    log("Appel vers $peer…")
                    btnHangup.isEnabled = true
                }
                Call.State.StreamsRunning -> {
                    log("En communication avec $peer")
                    btnAnswer.isEnabled = false
                }
                Call.State.Error -> log("Appel échoué : $message")
                Call.State.End, Call.State.Released -> {
                    if (state == Call.State.End) log("Appel terminé")
                    btnAnswer.isEnabled = false
                    btnHangup.isEnabled = core.callsNb > 0
                }
                else -> {}
            }
        }

        override fun onMessageReceived(core: Core, chatRoom: ChatRoom, message: ChatMessage) {
            log("SMS de ${message.fromAddress.username ?: "?"} : ${message.utf8Text ?: ""}")
        }
    }

    private val poll = object : Runnable {
        override fun run() {
            tick()
            ui.postDelayed(this, 300)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.main)
        prefs = getSharedPreferences("compte", MODE_PRIVATE)

        status = findViewById(R.id.status)
        journal = findViewById(R.id.journal)
        user = findViewById(R.id.user)
        secret = findViewById(R.id.secret)
        number = findViewById(R.id.number)
        text = findViewById(R.id.text)
        btnAnswer = findViewById(R.id.btn_answer)
        btnHangup = findViewById(R.id.btn_hangup)
        node = findViewById(R.id.node)
        spectrum = findViewById(R.id.spectrum)
        spectrumTitle = findViewById(R.id.spectrum_title)
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
        findViewById<Button>(R.id.btn_send).setOnClickListener { sendSms() }
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
        spectrum.onSwipe = {
            source = if (source == "bts") "ms" else "bts"
            spectrumTitle.text = if (source == "bts") "Spectre BTS (descendant)  ●○" else "Spectre MS (montant)  ○●"
        }

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
        core.removeListener(listener)
        core.stop()
        super.onDestroy()
    }

    // ── Plumbing ─────────────────────────────────────────────────────────────
    private fun log(line: String) {
        val t = SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date())
        journal.text = "$t  $line\n${journal.text}"
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
    private fun show(name: String) {
        tab = name
        findViewById<View>(R.id.panel_join).visibility = if (name == "phone" && !enrolled) View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_phone).visibility = if (name == "phone" && enrolled) View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_radio).visibility = if (name == "radio") View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_node).visibility = if (name == "node") View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_account).visibility = if (name == "account") View.VISIBLE else View.GONE
        showStatus()
        etatAt = 0
        cellsAt = 0
        if (name == "node" && host.isEmpty()) spectrum.fail("pas de nœud : son adresse dans Compte")
    }

    private fun applyMode() {
        val m = mode
        modeState.text = "Mode : ${listOf("Basique", "Medium", "Expert")[m]}"
        findViewById<View>(R.id.tab_radio).visibility = if (m >= 1) View.VISIBLE else View.GONE
        findViewById<View>(R.id.tab_node).visibility = if (m >= 1) View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_console).visibility = if (m == 2) View.VISIBLE else View.GONE
        findViewById<View>(R.id.panel_trunk).visibility = if (m == 2) View.VISIBLE else View.GONE
        if (m == 0 && (tab == "radio" || tab == "node")) show("phone")
    }

    private fun tick() {
        val now = SystemClock.elapsedRealtime()
        if (tab == "radio" && now - cellsAt > 2000) {
            cellsAt = now
            refreshCells()
        }
        if (tab != "node" || host.isEmpty()) return
        val h = host
        if (!psdBusy) {
            psdBusy = true
            val s = source
            bg({ http("http://$h:8081/psd?src=$s", null, 3000) }) { d, err ->
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
        if (now - etatAt > 5000) {
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
    }

    private fun showEtat(d: JSONObject?, err: String?) {
        val procs = d?.optJSONArray("procs")
        if (procs == null) {
            services.text = "Services : ${why(d, err)}"
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
        prefs.edit().clear().putString("node", keepNode).putInt("mode", keepMode).apply()
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
        val to = peer()
        val body = text.text.toString()
        if (number.text.isBlank() || to == null || body.isEmpty()) {
            log("Numéro ou message manquant")
            return
        }
        // SIP MESSAGE, one basic chat room per peer.
        val room = core.getChatRoom(to)
        if (room == null) {
            log("SMS impossible vers ${to.username}")
            return
        }
        room.createMessageFromUtf8(body).send()
        log("SMS à ${to.username} : $body")
        text.setText("")
    }
}
