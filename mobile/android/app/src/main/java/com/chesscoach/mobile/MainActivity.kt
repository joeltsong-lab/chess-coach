package com.chesscoach.mobile

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject

/**
 * 主界面：全屏 WebView 加载电脑上的 /mobile 页面。
 *
 * - 首次启动（没存过地址）→ 先跳 ConfigActivity
 * - 之后启动 → 直接加载已保存的地址
 * - 返回键 → WebView 能后退就后退，否则退出
 * - 页面脚本执行前注入 JS，给所有 /api/mobile/* 请求自动加 X-Mobile-Token 头
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar

    private var scriptHandler: WebViewCompat.ScriptHandler? = null
    private var errorDialogVisible = false

    /** 老版本 WebView 不支持 document-start 注入，需要退回 onPageStarted 注入 */
    private val documentStartSupported =
        WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)

    private val configLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        if (Prefs.isConfigured(this)) {
            loadPage()
        } else {
            finish()   // 没配置就没有能显示的页面，直接退出
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        progressBar = findViewById(R.id.progressBar)

        setupWebView()
        setupBackHandling()

        if (Prefs.isConfigured(this)) {
            loadPage()
        } else {
            openConfig()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true          // localStorage：页面存设置用
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = true
        settings.setSupportZoom(false)
        settings.builtInZoomControls = false
        settings.mediaPlaybackRequiresUserGesture = false
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        // User-Agent 追加来源标识，方便服务端日志区分
        settings.userAgentString = (settings.userAgentString ?: "") + " ChessCoach/Android"

        webView.setBackgroundColor(getColor(R.color.bg))

        webView.webViewClient = object : WebViewClient() {

            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                errorDialogVisible = false
                if (!documentStartSupported) view?.evaluateJavascript(tokenScript(), null)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                if (!documentStartSupported) view?.evaluateJavascript(tokenScript(), null)
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                if (request?.isForMainFrame == true) {
                    showLoadFailed(error?.description?.toString() ?: getString(R.string.load_failed_title))
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                progressBar.progress = newProgress
                progressBar.visibility =
                    if (newProgress in 1..99) View.VISIBLE else View.GONE
            }
        }
    }

    private fun setupBackHandling() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()   // 交回系统：退出 App
                }
            }
        })
    }

    /** 在页面脚本执行前装入拦截脚本（只在支持时调用一次） */
    private fun applyTokenInjection() {
        if (!documentStartSupported) return
        scriptHandler?.remove()
        scriptHandler = WebViewCompat.addDocumentStartJavaScript(webView, tokenScript(), setOf("*"))
    }

    private fun tokenScript(): String =
        TOKEN_SCRIPT.replace("__TOKEN__", JSONObject.quote(Prefs.effectiveToken(this)))

    private fun loadPage() {
        if (!Prefs.isConfigured(this)) {
            openConfig()
            return
        }
        errorDialogVisible = false
        applyTokenInjection()
        webView.loadUrl(Prefs.pageUrl(this))
    }

    private fun openConfig() {
        configLauncher.launch(Intent(this, ConfigActivity::class.java))
    }

    private fun showLoadFailed(detail: String) {
        if (errorDialogVisible || isFinishing) return
        errorDialogVisible = true
        progressBar.visibility = View.GONE
        AlertDialog.Builder(this)
            .setTitle(R.string.load_failed_title)
            .setMessage(getString(R.string.load_failed_msg, detail))
            .setCancelable(false)
            .setPositiveButton(R.string.btn_retry) { _, _ -> loadPage() }
            .setNegativeButton(R.string.btn_settings) { _, _ -> openConfig() }
            .show()
    }

    companion object {
        /**
         * 注入脚本：包装 fetch / XMLHttpRequest，凡是打到 /api/mobile/ 的请求
         * 都补上 X-Mobile-Token 头（页面自己的请求头不受影响）。
         * __TOKEN__ 会被替换成 JSON 字符串字面量。
         */
        private val TOKEN_SCRIPT = """
            (function () {
              var TOKEN = __TOKEN__;
              if (!TOKEN) return;

              function toUrl(u) {
                try { return new URL(u, location.href); } catch (e) { return null; }
              }
              function isApi(u) {
                return !!u && u.pathname.indexOf('/api/mobile/') === 0;
              }

              // 1) fetch
              var nativeFetch = window.fetch;
              if (nativeFetch) {
                window.fetch = function (input, init) {
                  try {
                    init = init || {};
                    var url = (typeof input === 'string') ? input : (input && input.url);
                    if (isApi(toUrl(url))) {
                      var h = new Headers(init.headers || (input && input.headers) || {});
                      h.set('X-Mobile-Token', TOKEN);
                      init.headers = h;
                    }
                  } catch (e) {}
                  return nativeFetch.call(this, input, init);
                };
              }

              // 2) XMLHttpRequest
              var nativeOpen = XMLHttpRequest.prototype.open;
              XMLHttpRequest.prototype.open = function (method, url) {
                this.__ccUrl = url;
                return nativeOpen.apply(this, arguments);
              };
              var nativeSend = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.send = function (body) {
                try {
                  if (isApi(toUrl(this.__ccUrl))) {
                    this.setRequestHeader('X-Mobile-Token', TOKEN);
                  }
                } catch (e) {}
                return nativeSend.apply(this, arguments);
              };
            })();
        """.trimIndent()
    }
}
