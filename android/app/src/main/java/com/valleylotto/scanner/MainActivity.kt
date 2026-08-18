package com.valleylotto.scanner

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Context
import android.os.Bundle
import android.text.InputType
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

/**
 * A thin, purpose-built shell around the Valley Lotto web app.
 *
 * Why an app at all, when the site works in a browser: a counting shift wants a
 * home-screen icon, no browser chrome to mis-tap, a screen that doesn't sleep
 * mid-count, and a session that survives for weeks. The scanning itself needs no
 * native code — a retail scan gun types the barcode like a keyboard, so the
 * page's own input receives it exactly as it does on a desktop.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var prefs: android.content.SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        web = WebView(this)
        setContentView(web)
        configureWebView()

        // A count is a minute of scanning with no touches — don't sleep partway.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        val url = prefs.getString(KEY_URL, null)
        if (url.isNullOrBlank()) promptForServer() else web.loadUrl(url)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        web.settings.apply {
            javaScriptEnabled = true          // the scan page is driven by JS
            domStorageEnabled = true
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            // The site sets its own readable sizes; let the device's font scale
            // apply but don't let pinch-zoom strand a clerk mid-count.
            builtInZoomControls = false
        }
        // Sessions must survive app restarts — otherwise a clerk re-enters a PIN
        // every morning, which is exactly what the app is meant to avoid.
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)

        // Long-press anywhere to re-point the device at a different store. Rare,
        // but it beats uninstalling the app to fix a typo'd address.
        web.setOnLongClickListener { promptForServer(); true }

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView, req: WebResourceRequest): Boolean {
                // Keep navigation inside the configured site; anything else is a
                // mis-tap, not a destination.
                val host = req.url.host ?: return true
                val ours = android.net.Uri.parse(prefs.getString(KEY_URL, "") ?: "").host
                return if (host == ours) false else true
            }

            override fun onReceivedError(v: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) showOfflineNotice()
            }
        }
    }

    /** First launch: ask where the store's site lives. */
    private fun promptForServer() {
        val input = EditText(this).apply {
            hint = "https://your-store.up.railway.app"
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setText(prefs.getString(KEY_URL, "") ?: "")
        }
        val pad = (resources.displayMetrics.density * 20).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, 0)
            addView(TextView(context).apply {
                text = getString(R.string.server_help)
                setPadding(0, 0, 0, pad / 2)
            })
            addView(input)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.server_title)
            .setView(box)
            .setCancelable(false)
            .setPositiveButton(R.string.save) { _, _ ->
                var url = input.text.toString().trim()
                if (url.isNotEmpty()) {
                    // Plain http is blocked by usesCleartextTraffic=false, so
                    // default to https rather than failing with a blank screen.
                    if (!url.startsWith("http://") && !url.startsWith("https://")) {
                        url = "https://$url"
                    }
                    prefs.edit().putString(KEY_URL, url).apply()
                    web.loadUrl(url)
                } else {
                    promptForServer()
                }
            }
            .show()
    }

    private fun showOfflineNotice() {
        AlertDialog.Builder(this)
            .setTitle(R.string.offline_title)
            .setMessage(R.string.offline_body)
            .setPositiveButton(R.string.retry) { _, _ ->
                web.loadUrl(prefs.getString(KEY_URL, "") ?: "")
            }
            .setNegativeButton(R.string.change_server) { _, _ -> promptForServer() }
            .show()
    }

    companion object {
        private const val PREFS = "valley_lotto"
        private const val KEY_URL = "server_url"
    }
}
