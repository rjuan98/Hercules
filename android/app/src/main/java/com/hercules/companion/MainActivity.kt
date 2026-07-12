package com.hercules.companion

import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import com.hercules.companion.databinding.ActivityMainBinding

/** A tela principal É o Hércules de verdade, dentro de um WebView — não existe
 *  login separado nem senha própria do app. Só o botão "Entrar com Google"
 *  precisa de um desvio de 2 segundos por uma aba seguro do Chrome (o Google
 *  não permite login dentro de WebView comum); o resto acontece direto aqui. */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val appHost = Uri.parse(ApiClient.BASE_URL).host

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val webView = binding.webView
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                // O login do Google só funciona numa aba de navegador de verdade —
                // por isso interceptamos ANTES de sair do nosso próprio domínio.
                if (url.host == appHost && url.path == "/login/google") {
                    abrirLoginGoogle()
                    return true
                }
                return false
            }

            override fun onPageFinished(view: WebView, url: String) {
                super.onPageFinished(view, url)
                if (url == "${ApiClient.BASE_URL}/") {
                    // Login por e-mail/senha aconteceu direto aqui no WebView —
                    // pega o token da mesma sessão que acabou de logar.
                    val cookie = CookieManager.getInstance().getCookie(ApiClient.BASE_URL)
                    if (!cookie.isNullOrBlank()) {
                        ApiClient.fetchTokenWithCookie(cookie) { token, nome ->
                            if (token != null) Prefs.saveLogin(this@MainActivity, token, nome)
                        }
                    }
                }
                atualizarBotaoNotificacoes()
            }
        }

        binding.btnAtivarNotificacoes.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        if (!handleAppLinkIntent(intent)) {
            webView.loadUrl(ApiClient.BASE_URL)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleAppLinkIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        atualizarBotaoNotificacoes()
    }

    private fun abrirLoginGoogle() {
        val url = "${ApiClient.BASE_URL}/login/google?from_app=1"
        CustomTabsIntent.Builder().build().launchUrl(this, Uri.parse(url))
    }

    /** Volta do login do Google via App Link: pega o token (direto da URL,
     *  já que a aba do Chrome não compartilha cookies com o WebView) e usa o
     *  código de uso único para o WebView virar uma sessão de verdade. */
    private fun handleAppLinkIntent(intent: Intent?): Boolean {
        val data = intent?.data ?: return false
        if (data.host != appHost || data.path != "/app/entrou") return false

        val token = data.getQueryParameter("token")
        val code = data.getQueryParameter("code")
        if (token != null) Prefs.saveLogin(this, token, Prefs.getNome(this))
        if (!code.isNullOrBlank()) {
            binding.webView.loadUrl("${ApiClient.BASE_URL}/entrar-automatico?code=$code")
        } else {
            binding.webView.loadUrl(ApiClient.BASE_URL)
        }
        return true
    }

    private fun isListenerAtivo(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners")
        if (flat.isNullOrEmpty()) return false
        return flat.split(":").any { entry ->
            val cn = ComponentName.unflattenFromString(entry)
            cn != null && cn.packageName == packageName
        }
    }

    private fun atualizarBotaoNotificacoes() {
        binding.btnAtivarNotificacoes.visibility = if (isListenerAtivo()) View.GONE else View.VISIBLE
    }

    override fun onBackPressed() {
        if (binding.webView.canGoBack()) {
            binding.webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
