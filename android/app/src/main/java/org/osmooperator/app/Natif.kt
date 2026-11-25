// THE BENCH IN THE NATIVE APPS OF THE PHONE.
//
// With the proxy on, the calls of the bench number no longer show in this
// app: they ring in the normal Phone app (Google's), with its call screen and
// its call log, and the normal dialer can place a call through the bench.
// Android has a mechanism made for it: this app registers a "calling account"
// (PhoneAccount, CAPABILITY_CALL_PROVIDER) and a ConnectionService; Telecom
// then drives the native UI and tells us what the user does. The voice is
// still carried by this app (SIP, Linphone) - which is why it crosses.
// The user enables the account ONCE: Phone app > Settings > Calls > Calling
// accounts (the "Comptes téléphoniques" button opens that screen).
//
// SMS cannot follow: only the default SMS app may write to the system SMS
// store, so a bench SMS cannot appear in Google Messages. With the proxy on
// it comes as a system notification instead of a bubble in this app.
package org.osmooperator.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.telecom.Connection
import android.telecom.ConnectionRequest
import android.telecom.ConnectionService
import android.telecom.DisconnectCause
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.Reason

object Bench {
    /** The SIP stack, shared by the activity and the Telecom service. */
    var core: Core? = null
    private var app: Context? = null
    private var hooked = false
    private var notifId = 100

    /** The SIP call that just rang, waiting for the Connection Telecom will ask for. */
    var pendingIncoming: Call? = null
    val links = HashMap<Call, BenchConnection>()

    fun prefs(ctx: Context) = ctx.getSharedPreferences("compte", Context.MODE_PRIVATE)
    fun native(ctx: Context) = prefs(ctx).getBoolean("natif", false)

    /** The native Phone app really has the calls: proxy on AND the account enabled
     *  by the user. Until then the calls stay in this app - Telecom silently
     *  ignores an incoming call on a disabled account, it would ring nowhere. */
    fun nativeActive(ctx: Context) = native(ctx) && accountEnabled(ctx)

    // ── Ringing in this app (no native UI for the call) ─────────────────────
    private var ringtone: android.media.Ringtone? = null

    fun ring(ctx: Context, from: String) {
        stopRing()
        try {
            val uri = android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_RINGTONE)
            ringtone = android.media.RingtoneManager.getRingtone(ctx.applicationContext, uri)?.also { it.play() }
        } catch (e: Exception) { }
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("appel", "Appels du banc", NotificationManager.IMPORTANCE_HIGH))
        val open = PendingIntent.getActivity(ctx, 1,
            Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, "appel")
            .setContentTitle("Appel entrant de $from")
            .setContentText("Touchez pour répondre dans Network Monitor")
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setCategory(Notification.CATEGORY_CALL)
            .setFullScreenIntent(open, true)              // wakes the screen and shows the app
            .setContentIntent(open).setOngoing(true)
            .build()
        try { nm.notify(42, n) } catch (e: SecurityException) { }
    }

    fun stopRing() {
        try { ringtone?.stop() } catch (e: Exception) { }
        ringtone = null
        app?.getSystemService(NotificationManager::class.java)?.cancel(42)
    }

    fun handle(ctx: Context) =
        PhoneAccountHandle(ComponentName(ctx.applicationContext, BenchConnectionService::class.java), "banc")

    private fun telecom(ctx: Context) = ctx.getSystemService(Context.TELECOM_SERVICE) as TelecomManager

    /** Declare the bench number as a calling account of the phone. */
    fun register(ctx: Context) {
        val num = prefs(ctx).getString("msisdn", "") ?: ""
        val acc = PhoneAccount.builder(handle(ctx), "Banc $num")
            .setCapabilities(PhoneAccount.CAPABILITY_CALL_PROVIDER)
            .addSupportedUriScheme(PhoneAccount.SCHEME_TEL)
            .setAddress(Uri.fromParts(PhoneAccount.SCHEME_TEL, num, null))
            .setShortDescription("Numéro $num du nœud osmo-operator")
            .build()
        telecom(ctx).registerPhoneAccount(acc)
    }

    fun unregister(ctx: Context) {
        try { telecom(ctx).unregisterPhoneAccount(handle(ctx)) } catch (e: Exception) { }
    }

    /** True once the user enabled the account in the Phone app.
     *
     *  The list of call-capable accounts first: it only holds ENABLED accounts
     *  and asks for READ_PHONE_STATE, which the app has. getPhoneAccount() alone
     *  was wrong: since Android 12 it wants READ_PHONE_NUMBERS for an account
     *  that is not self-managed, and without it throws (or returns null) - the
     *  account looked disabled while it was enabled, and even the default one,
     *  so the calls kept ringing in this app instead of the Phone app. */
    @android.annotation.SuppressLint("MissingPermission")
    fun accountEnabled(ctx: Context): Boolean {
        val h = handle(ctx)
        val listed = try { telecom(ctx).callCapablePhoneAccounts.contains(h) } catch (e: Exception) { false }
        if (listed) return true
        return try { telecom(ctx).getPhoneAccount(h)?.isEnabled == true } catch (e: Exception) { false }
    }

    /** Hook the SIP stack once: its calls go to Telecom while the proxy is on. */
    fun hook(ctx: Context, c: Core) {
        app = ctx.applicationContext
        core = c
        if (hooked) return
        hooked = true
        c.addListener(object : CoreListenerStub() {
            override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
                val a = app ?: return
                if (!nativeActive(a)) return                   // not ours: the app shows and rings the call
                when (state) {
                    Call.State.IncomingReceived -> {
                        val num = call.remoteAddress.username ?: "inconnu"
                        pendingIncoming = call
                        try {
                            telecom(a).addNewIncomingCall(handle(a), Bundle().apply {
                                putParcelable(TelecomManager.EXTRA_INCOMING_CALL_ADDRESS,
                                    Uri.fromParts(PhoneAccount.SCHEME_TEL, num, null))
                            })
                        } catch (e: Exception) {
                            // account not enabled in the Phone app: the call would ring nowhere
                            pendingIncoming = null
                            call.decline(Reason.NotImplemented)
                            notify(a, "Appel du banc manqué ($num)",
                                "Activez le compte « Banc » dans l'appli Téléphone (Comptes téléphoniques).")
                        }
                    }
                    Call.State.StreamsRunning -> links[call]?.setActive()
                    Call.State.Error -> finish(call, DisconnectCause(DisconnectCause.ERROR, message))
                    Call.State.End, Call.State.Released -> finish(call, DisconnectCause(DisconnectCause.REMOTE))
                    else -> {}
                }
            }
        })
    }

    private fun finish(call: Call, cause: DisconnectCause) {
        if (pendingIncoming === call) pendingIncoming = null
        val c = links.remove(call) ?: return
        if (c.state != Connection.STATE_DISCONNECTED) c.setDisconnected(cause)
        c.destroy()
    }

    /** A bench SMS, or a notice, as a system notification (proxy on). */
    fun notify(ctx: Context, title: String, text: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("banc", "SMS et avis du banc", NotificationManager.IMPORTANCE_HIGH))
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, "banc")
            .setContentTitle(title).setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentIntent(open).setAutoCancel(true)
            .build()
        try { nm.notify(notifId++, n) } catch (e: SecurityException) { }
    }
}

/** One call of the bench, as the native Phone app sees it. */
class BenchConnection(private val call: Call?) : Connection() {
    private fun over(cause: Int) {
        if (call != null) Bench.links.remove(call)
        setDisconnected(DisconnectCause(cause))
        destroy()
    }

    override fun onAnswer() { call?.accept() }
    override fun onReject() { call?.decline(Reason.Declined); over(DisconnectCause.REJECTED) }
    override fun onDisconnect() { call?.terminate(); over(DisconnectCause.LOCAL) }
    override fun onAbort() { call?.terminate(); over(DisconnectCause.CANCELED) }
    override fun onPlayDtmfTone(c: Char) { call?.sendDtmf(c) }
    override fun onHold() { call?.pause(); setOnHold() }
    override fun onUnhold() { call?.resume(); setActive() }
}

class BenchConnectionService : ConnectionService() {
    private fun wire(c: BenchConnection, address: Uri?) {
        c.setAddress(address, TelecomManager.PRESENTATION_ALLOWED)
        c.setAudioModeIsVoip(true)                       // the voice is ours (SIP), not the modem's
        c.connectionCapabilities = Connection.CAPABILITY_HOLD or Connection.CAPABILITY_SUPPORT_HOLD or
            Connection.CAPABILITY_MUTE
    }

    /** The bench calls: Telecom asks for the object behind the ringing screen. */
    override fun onCreateIncomingConnection(account: PhoneAccountHandle?, request: ConnectionRequest): Connection {
        val call = Bench.pendingIncoming
            ?: return Connection.createFailedConnection(DisconnectCause(DisconnectCause.ERROR, "appel déjà fini"))
        Bench.pendingIncoming = null
        @Suppress("DEPRECATION")
        val from = request.extras?.getParcelable<Uri>(TelecomManager.EXTRA_INCOMING_CALL_ADDRESS) ?: request.address
        val c = BenchConnection(call)
        wire(c, from)
        c.setRinging()
        Bench.links[call] = c
        return c
    }

    /** The native dialer places a call through the "Banc" account. */
    override fun onCreateOutgoingConnection(account: PhoneAccountHandle?, request: ConnectionRequest): Connection {
        val core = Bench.core
            ?: return Connection.createFailedConnection(DisconnectCause(DisconnectCause.ERROR, "l'appli du banc est fermée"))
        val num = (request.address?.schemeSpecificPart ?: "").replace(" ", "").replace("-", "")
        val domain = Bench.prefs(this).getString("sip_domain", "") ?: ""
        val to = Factory.instance().createAddress("sip:$num@$domain")
        val call = (if (num.isEmpty() || to == null) null else core.inviteAddress(to))
            ?: return Connection.createFailedConnection(DisconnectCause(DisconnectCause.ERROR, "numéro refusé par le nœud"))
        val c = BenchConnection(call)
        wire(c, request.address)
        c.setDialing()
        Bench.links[call] = c
        return c
    }

    override fun onCreateIncomingConnectionFailed(account: PhoneAccountHandle?, request: ConnectionRequest?) {
        Bench.pendingIncoming?.decline(Reason.Busy)
        Bench.pendingIncoming = null
    }
}
