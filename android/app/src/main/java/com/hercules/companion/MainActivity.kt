package com.hercules.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.hercules.companion.databinding.ActivityMainBinding

/** A tela principal É o Hércules de verdade, dentro de um WebView — não existe
 *  login separado nem senha própria do app. Só o botão "Entrar com Google"
 *  precisa de um desvio de 2 segundos por uma aba seguro do Chrome (o Google
 *  não permite login dentro de WebView comum); o resto acontece direto aqui. */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val appHost = Uri.parse(ApiClient.BASE_URL).host
    private val canalTeste = "herc_teste"

    private val pedirPermissaoNotificacao = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { concedida -> if (concedida) postarNotificacaoTeste() }

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
        binding.btnTestarCaptura.setOnClickListener { testarCaptura() }

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

    /** Posta uma notificação de teste do próprio app. Se ela aparecer no
     *  Hércules, prova que a captura (leitura + rede + servidor) funciona de
     *  ponta a ponta — o que sobrar de errado é só o pacote do banco. */
    private fun testarCaptura() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            pedirPermissaoNotificacao.launch(Manifest.permission.POST_NOTIFICATIONS)
            return
        }
        postarNotificacaoTeste()
    }

    private fun postarNotificacaoTeste() {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && manager.getNotificationChannel(canalTeste) == null) {
            manager.createNotificationChannel(
                NotificationChannel(canalTeste, "Teste do Hércules", NotificationManager.IMPORTANCE_DEFAULT)
            )
        }
        val notif = NotificationCompat.Builder(this, canalTeste)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("Teste do Hércules")
            .setContentText("Compra de R$ 1,23 em TESTE CAPTURA")
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(this).notify(9999, notif)
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
