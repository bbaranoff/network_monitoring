// THE PROXY: this phone lends its SIM card to the bench.
//
// Once activated (tab "Proxy"), with the SIP account of the node as identity:
//   - an SMS received by the SIM (alongside the normal Messages app, which
//     keeps receiving it) is posted to the node, which delivers it to the
//     bench number chosen in the tab;
//   - an SMS of the bench to an OUTSIDE number (06..., +33..., 00...) waits in
//     a queue on the node; this phone takes it and sends it with its SIM;
//   - when the SIM rings, the bench is told who is calling.
// THE VOICE DOES NOT CROSS: Android gives no app access to the audio of a
// cellular call. The call itself stays in the normal Phone app.
//
// The node side is osmo-inscription (osmo-operator): POST /proxy/etat,
// POST /proxy/entrant, GET /proxy/sortants. The SIP password of the account is
// the token. Plain http: local network or WireGuard only, like the rest.
package org.osmooperator.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.Telephony
import android.telephony.SmsManager
import android.telephony.TelephonyManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlin.concurrent.thread

object Proxy {
    private const val PORT = 8088

    fun prefs(ctx: Context) = ctx.getSharedPreferences("compte", Context.MODE_PRIVATE)
    fun enabled(ctx: Context) = prefs(ctx).getBoolean("proxy", false)

    /** POST (body) or GET (null) on the node; null when it cannot be reached. */
    fun call(ctx: Context, path: String, body: JSONObject?): JSONObject? {
        val p = prefs(ctx)
        val host = p.getString("node", "") ?: ""
        if (host.isEmpty()) return null
        return try {
            val c = URL("http://$host:$PORT$path").openConnection() as HttpURLConnection
            c.connectTimeout = 6000
            c.readTimeout = 15000
            if (body != null) {
                c.requestMethod = "POST"
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val s = if (c.responseCode in 200..299) c.inputStream else c.errorStream
            JSONObject(s?.bufferedReader()?.readText() ?: "{}")
        } catch (e: Exception) {
            null
        }
    }

    /** The identity of this phone on the node: its SIP account. */
    fun who(ctx: Context): JSONObject {
        val p = prefs(ctx)
        return JSONObject().put("user", p.getString("sip_user", "")).put("mdp", p.getString("sip_password", ""))
    }

    fun query(ctx: Context): String {
        val p = prefs(ctx)
        fun e(k: String) = URLEncoder.encode(p.getString(k, "") ?: "", "UTF-8")
        return "user=${e("sip_user")}&mdp=${e("sip_password")}"
    }

    /** Announce (or withdraw) this phone as the proxy of the node. */
    fun announce(ctx: Context, on: Boolean): JSONObject? =
        call(ctx, "/proxy/etat", who(ctx).put("actif", on).put("dest", prefs(ctx).getString("proxy_dest", "") ?: ""))

    /** Something that came through the SIM, handed to the bench. */
    fun incoming(ctx: Context, from: String, text: String): Boolean =
        call(ctx, "/proxy/entrant", who(ctx).put("de", from).put("texte", text))?.optBoolean("ok") == true

    fun sendSms(ctx: Context, to: String, text: String) {
        @Suppress("DEPRECATION")
        val sm = if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(SmsManager::class.java) else SmsManager.getDefault()
        sm.sendMultipartTextMessage(to, null, sm.divideMessage(text), null, null)
    }
}

/** An SMS arrives on the SIM: the normal Messages app gets it, and so does the bench. */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION || !Proxy.enabled(ctx)) return
        val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        // one SMS may come in several parts: one message per sender
        val bySender = LinkedHashMap<String, StringBuilder>()
        for (m in parts) bySender.getOrPut(m.originatingAddress ?: "?") { StringBuilder() }.append(m.messageBody ?: "")
        val pending = goAsync()
        thread {
            try {
                for ((from, text) in bySender) Proxy.incoming(ctx, from, text.toString())
            } finally {
                pending.finish()
            }
        }
    }
}

/** The SIM rings: the bench is told who is calling (the voice stays in the Phone app). */
class CallReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED || !Proxy.enabled(ctx)) return
        if (intent.getStringExtra(TelephonyManager.EXTRA_STATE) != TelephonyManager.EXTRA_STATE_RINGING) return
        // Android sends the broadcast twice; only the one that carries the number is used
        @Suppress("DEPRECATION")
        val from = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER) ?: return
        val pending = goAsync()
        thread {
            try {
                Proxy.incoming(ctx, from, "Appel entrant de $from sur la SIM du proxy (la voix reste sur le smartphone)")
            } finally {
                pending.finish()
            }
        }
    }
}

/** Keeps the proxy alive with the app closed: takes the outgoing SMS, gives signs of life. */
class ProxyService : Service() {
    @Volatile private var running = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("proxy", "Proxy du banc", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, "proxy")
            .setContentTitle("Banc en ligne")
            .setContentText("Votre numéro du banc reste joignable, appli fermée.")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(7, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        else startForeground(7, n)
        if (!running) {
            running = true
            thread { loop() }
        }
        return START_STICKY
    }

    private fun loop() {
        var announced = 0L
        // Also the keeper of the process while the bench lives in the native
        // apps (Natif.kt): the SIP stack must stay up with the screen closed.
        // ... and of the line itself: with an account, calls must ring app closed.
        while (running && (Proxy.enabled(this) || Bench.native(this) || Proxy.prefs(this).contains("sip_user"))) {
            val now = System.currentTimeMillis()
            if (!Proxy.enabled(this)) {
                try { Thread.sleep(5000) } catch (e: InterruptedException) { break }
                continue
            }
            if (now - announced > 30000) {
                Proxy.announce(this, true)
                announced = now
            }
            val r = Proxy.call(this, "/proxy/sortants?${Proxy.query(this)}", null)
            val list = r?.optJSONArray("sms")
            if (list != null) for (i in 0 until list.length()) {
                val s = list.getJSONObject(i)
                try {
                    Proxy.sendSms(this, s.optString("vers"), s.optString("texte"))
                } catch (e: Exception) {
                    // a refused SMS (no SIM, permission withdrawn) must not stop the others
                }
            }
            try { Thread.sleep(5000) } catch (e: InterruptedException) { break }
        }
        Proxy.announce(this, false)
        running = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        super.onDestroy()
    }
}
