// The spectrum of one I/Q source of the node: the PSD on top, the waterfall
// (turbo palette) below. Rows go down. Swipe left / right: the other source.
package org.osmooperator.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

class SpectrumView(context: Context, attrs: AttributeSet?) : View(context, attrs) {
    private val cols = 256
    private val rows = 150
    private val pixels = IntArray(cols * rows) { TURBO[0] }
    private val bitmap = Bitmap.createBitmap(cols, rows, Bitmap.Config.ARGB_8888)
    private var trace = FloatArray(0)
    private var hold = FloatArray(0)
    private var lo = -100.0
    private var hi = -50.0
    private var f0 = -541.0
    private var f1 = 541.0
    private var peakCol = 0
    private var peakDb = 0.0
    private var label = ""
    private var message = "en attente du nœud…"
    private var downX = 0f

    /** -1: swipe to the left (next source), +1: to the right (previous). */
    var onSwipe: ((Int) -> Unit)? = null

    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 3f; color = Color.WHITE
    }
    private val holdPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 2f; color = Color.argb(70, 255, 255, 255)
    }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val grid = Paint().apply { color = Color.argb(28, 255, 255, 255); strokeWidth = 1f }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(220, 255, 255, 255); textSize = 30f }
    private val small = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(150, 255, 255, 255); textSize = 24f }
    private val green = Color.rgb(0x57, 0xe3, 0x89)
    private val peakDot = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = green }
    private val peakText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = green; textSize = 28f; typeface = android.graphics.Typeface.DEFAULT_BOLD
    }
    private val peakPill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(217, 5, 26, 13) }

    fun fail(why: String) {
        message = why
        invalidate()
    }

    fun push(psd: DoubleArray, first: Double, last: Double, name: String) {
        if (psd.isEmpty()) return fail("spectre vide")
        if ((psd.maxOrNull() ?: 0.0) - (psd.minOrNull() ?: 0.0) < 0.5) {
            // flat floor: the FIFO receives nothing
            trace = FloatArray(0)
            return fail(
                if (name.startsWith("MS")) "Le mobile n'émet pas (au repos) : le montant s'anime pendant un appel, un SMS ou un Location Update."
                else "$name : aucun signal (source muette)"
            )
        }
        val n = psd.size
        // one column = the maximum of its group: a narrow peak does not vanish
        val c = DoubleArray(cols) { i ->
            val a = i * n / cols
            val b = max(a + 1, (i + 1) * n / cols)
            var m = psd[a]
            for (k in a until min(b, n)) m = max(m, psd[k])
            m
        }
        val sorted = c.sortedArray()
        val floor = sorted[cols / 10] - 4
        val top = sorted[cols - 1] + 4
        // IMMEDIATE ATTACK, SLOW RELEASE: smoothed both ways, the scale lagged
        // behind a burst (uplink) - clipped trace, peak label off the axis.
        val t2 = max(top, floor + 20)
        hi = if (t2 > hi) t2 else hi + (t2 - hi) * 0.05
        lo = if (floor < lo) floor else lo + (floor - lo) * 0.05
        val range = max(1.0, hi - lo)
        trace = FloatArray(cols) { i -> ((c[i] - lo) / range).coerceIn(0.0, 1.0).toFloat() }
        hold = if (hold.size == cols) FloatArray(cols) { i -> max(hold[i] * 0.985f, trace[i]) } else trace.copyOf()
        System.arraycopy(pixels, 0, pixels, cols, cols * (rows - 1))
        for (i in 0 until cols) pixels[i] = TURBO[(trace[i] * 255).toInt()]
        bitmap.setPixels(pixels, 0, cols, 0, 0, cols, rows)
        peakCol = c.indices.maxByOrNull { c[it] } ?: 0
        peakDb = c[peakCol]
        f0 = first; f1 = last; label = name; message = ""
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        val th = h * 0.36f
        canvas.drawColor(Color.rgb(8, 10, 18))
        canvas.drawBitmap(bitmap, null, RectF(0f, th, w, h), bitmapPaint)
        if (trace.isEmpty()) {
            // word-wrapped in the width
            val lines = mutableListOf<String>()
            var cur = ""
            for (word in message.split(" ")) {
                val t = if (cur.isEmpty()) word else "$cur $word"
                if (cur.isNotEmpty() && text.measureText(t) > w - 48f) { lines.add(cur); cur = word } else cur = t
            }
            lines.add(cur)
            var yy = th / 2 - (lines.size - 1) * 20f
            for (l in lines) { canvas.drawText(l, (w - text.measureText(l)) / 2, yy, text); yy += 40f }
            return
        }
        for (k in 1..3) {
            val y = th * k / 4
            canvas.drawLine(0f, y, w, y, grid)
            canvas.drawText("%.0f dB".format(hi - (hi - lo) * k / 4), 10f, y - 6, small)
        }
        val pad = 12f
        fun y(v: Float) = th - pad - v * (th - 2 * pad - 34f)
        fun x(i: Int) = w * i / (cols - 1)
        val area = Path().apply { moveTo(0f, th); for (i in 0 until cols) lineTo(x(i), y(trace[i])); lineTo(w, th); close() }
        fill.shader = LinearGradient(
            0f, pad, 0f, th,
            intArrayOf(alpha(TURBO[255], 215), alpha(TURBO[165], 160), alpha(TURBO[75], 100), alpha(TURBO[0], 60)),
            floatArrayOf(0f, 0.35f, 0.7f, 1f), Shader.TileMode.CLAMP
        )
        canvas.drawPath(area, fill)
        canvas.drawPath(Path().apply { for (i in 0 until cols) if (i == 0) moveTo(x(i), y(hold[i])) else lineTo(x(i), y(hold[i])) }, holdPaint)
        canvas.drawPath(Path().apply { for (i in 0 until cols) if (i == 0) moveTo(x(i), y(trace[i])) else lineTo(x(i), y(trace[i])) }, line)
        val px = x(peakCol)
        val py = y(trace[peakCol])
        // the peak in green, on a dark pill, never over the source title
        canvas.drawCircle(px, py, 8f, peakDot)
        val t = "%+.0f kHz · %.0f dB".format(f0 + (f1 - f0) * peakCol / (cols - 1), peakDb)
        val tw = peakText.measureText(t)
        val tx = (px - tw / 2).coerceIn(14f, w - tw - 18f)
        val ty = max(84f, py - 22f)
        canvas.drawRoundRect(RectF(tx - 12f, ty - 32f, tx + tw + 12f, ty + 12f), 16f, 16f, peakPill)
        canvas.drawText(t, tx, ty, peakText)
        canvas.drawText(label, w - text.measureText(label) - 12f, 34f, text)
        canvas.drawText("%+.0f kHz".format(f0), 12f, h - 12f, small)
        val r = "%+.0f kHz".format(f1)
        canvas.drawText(r, w - small.measureText(r) - 12f, h - 12f, small)
        if (message.isNotEmpty()) canvas.drawText(message, 12f, th + 34f, small)
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> { downX = e.x; parent?.requestDisallowInterceptTouchEvent(true) }
            MotionEvent.ACTION_UP -> {
                val dx = e.x - downX
                if (abs(dx) > width / 5f) onSwipe?.invoke(if (dx < 0) -1 else 1)
            }
        }
        return true
    }

    companion object {
        private fun alpha(c: Int, a: Int) = Color.argb(a, Color.red(c), Color.green(c), Color.blue(c))

        /** Turbo (Google), polynomial approximation. */
        val TURBO = IntArray(256) { i ->
            val x = i / 255.0
            val r = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))))
            val g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))))
            val b = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))))
            Color.rgb((r.coerceIn(0.0, 1.0) * 255).toInt(), (g.coerceIn(0.0, 1.0) * 255).toInt(), (b.coerceIn(0.0, 1.0) * 255).toInt())
        }
    }
}
