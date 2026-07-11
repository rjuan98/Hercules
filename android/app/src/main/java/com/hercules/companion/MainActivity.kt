package com.hercules.companion

import android.content.ComponentName
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.text.format.DateFormat
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import com.hercules.companion.databinding.ActivityMainBinding
import java.util.Date

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnEntrar.setOnClickListener { fazerLogin() }
        binding.btnAtivarNotificacoes.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        binding.btnTeste.setOnClickListener { enviarTeste() }
        binding.btnSair.setOnClickListener {
            Prefs.clear(this)
            atualizarTela()
        }

        atualizarTela()
    }

    override fun onResume() {
        super.onResume()
        atualizarTela()
    }

    private fun fazerLogin() {
        val email = binding.inputEmail.text.toString().trim()
        val senha = binding.inputSenha.text.toString()
        if (email.isEmpty() || senha.isEmpty()) {
            mostrarErro("Preencha e-mail e senha.")
            return
        }
        binding.btnEntrar.isEnabled = false
        binding.textErro.visibility = View.GONE
        ApiClient.login(email, senha) { token, nome, erro ->
            binding.btnEntrar.isEnabled = true
            if (token != null && nome != null) {
                Prefs.saveLogin(this, token, nome)
                atualizarTela()
            } else {
                mostrarErro(erro ?: "Não foi possível entrar.")
            }
        }
    }

    private fun mostrarErro(msg: String) {
        binding.textErro.text = msg
        binding.textErro.visibility = View.VISIBLE
    }

    private fun enviarTeste() {
        val token = Prefs.getToken(this) ?: return
        binding.textStatusCaptura.text = "Enviando teste…"
        val texto = "Teste do app Hércules: compra de R$ 1,23 em TESTE COMPANION"
        ApiClient.sendCapture(token, texto) { ok ->
            if (ok) Prefs.setLastCapture(this, texto)
            atualizarTela()
        }
    }

    private fun isListenerAtivo(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners")
        if (flat.isNullOrEmpty()) return false
        return flat.split(":").any { entry ->
            val cn = ComponentName.unflattenFromString(entry)
            cn != null && cn.packageName == packageName
        }
    }

    private fun atualizarTela() {
        val token = Prefs.getToken(this)
        val nome = Prefs.getNome(this)

        if (token == null) {
            binding.grupoLogin.visibility = View.VISIBLE
            binding.grupoConectado.visibility = View.GONE
            return
        }

        binding.grupoLogin.visibility = View.GONE
        binding.grupoConectado.visibility = View.VISIBLE
        binding.textConectado.text = "Conectado como $nome"

        val ativo = isListenerAtivo()
        binding.textStatusListener.text = if (ativo)
            "✓ Lendo notificações do banco"
        else
            "⚠ Ainda não ativado — toque no botão abaixo e marque o Hércules Captura"
        binding.btnAtivarNotificacoes.text =
            if (ativo) "Gerenciar acesso a notificações" else "Ativar acesso a notificações"

        val lastText = Prefs.getLastCaptureText(this)
        val lastTime = Prefs.getLastCaptureTime(this)
        binding.textStatusCaptura.text = if (lastText != null && lastTime > 0) {
            val hora = DateFormat.format("dd/MM HH:mm", Date(lastTime))
            "Última captura ($hora): $lastText"
        } else {
            "Nenhuma captura enviada ainda."
        }
    }
}
