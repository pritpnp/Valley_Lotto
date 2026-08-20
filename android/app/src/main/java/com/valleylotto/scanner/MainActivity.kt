package com.valleylotto.scanner

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Context
import android.os.Bundle
import android.text.InputType
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

/**
 * A thin, purpose-built shell around the Valley Lotto web app.
 *
 * Why an app at all, when the site works in a browser: a counting shift wants a
 * home-screen icon, no browser chrome to mis-tap, a screen that doesn't sleep
 * mid-count, and a session that survives for weeks. The scanning itself needs no
 * native code — a retail scan gun types the barcode like a keyboard, so the
 * page's own input receives it exactly as it does on a desktop.
 *
 * The one thing the web page genuinely cannot win on its own is the on-screen
 * keyboard. A page can ask Android not to show one; it cannot insist. Here we
 * can: the keyboard is held down from the app side and only allowed up when the
 * page says someone actually wants to type.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var prefs: android.content.SharedPreferences

    /** False only while the page has asked for a real keyboard. */
    private var suppressKeyboard = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        web = WebView(this)
        setContentView(web)
        configureWebView()

        // A count is a minute of scanning with no touches — don't sleep partway.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Hold the keyboard down. A scan gun is a keyboard already; the on-screen
        // one covers half the screen a clerk is trying to read and has no reason
        // to be there. The page lifts this deliberately when someone taps the ⌨
        // button, and puts it back afterwards.
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN)
        keepKeyboardDown()

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

        web.addJavascriptInterface(KeyboardBridge(), "ValleyLotto")

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

    /**
     * Re-hide the keyboard every time something manages to raise it.
     *
     * Setting the soft-input mode covers the window opening; this covers a
     * WebView field grabbing focus later, which is exactly what happens on the
     * scan screen. Without it the keyboard reappears on every scan.
     */
    private fun keepKeyboardDown() {
        ViewCompat.setOnApplyWindowInsetsListener(web) { _, insets ->
            if (suppressKeyboard && insets.isVisible(WindowInsetsCompat.Type.ime())) {
                hideKeyboardNow()
            }
            insets
        }
    }

    private fun hideKeyboardNow() {
        runOnUiThread {
            WindowInsetsControllerCompat(window, web).hide(WindowInsetsCompat.Type.ime())
            // Older devices don't always honour the insets controller.
            val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
            imm.hideSoftInputFromWindow(web.windowToken, 0)
        }
    }

    private fun showKeyboardNow() {
        runOnUiThread {
            web.requestFocus()
            WindowInsetsControllerCompat(window, web).show(WindowInsetsCompat.Type.ime())
            val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
            imm.showSoftInput(web, InputMethodManager.SHOW_IMPLICIT)
        }
    }

    /**
     * What the page is allowed to ask of the app.
     *
     * Deliberately tiny, and reachable only from the store's own site — the
     * WebView refuses to navigate anywhere else, so no other page can call this.
     */
    private inner class KeyboardBridge {
        /** The page is scanning: keep the keyboard down. */
        @JavascriptInterface
        fun hideKeyboard() {
            suppressKeyboard = true
            hideKeyboardNow()
        }

        /** Someone tapped the ⌨ button and wants to type. */
        @JavascriptInterface
        fun showKeyboard() {
            suppressKeyboard = false
            showKeyboardNow()
        }

        /** Lets the page say "the app is handling this" instead of guessing. */
        @JavascriptInterface
        fun hasKeyboardControl(): Boolean = true
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
