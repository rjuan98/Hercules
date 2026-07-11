package com.hercules.companion

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/** Lê as notificações dos apps de banco e manda o texto para o Hércules anotar sozinho.
 *  Só olha os pacotes desta lista — o resto do celular é ignorado. */
class NotificationCaptureService : NotificationListenerService() {

    companion object {
        val BANK_PACKAGES = setOf(
            "com.nu.production",           // Nubank
            "com.itau",                    // Itaú
            "com.itau.investimentos",
            "br.com.intermedium",          // Banco Inter
            "com.c6bank.app",              // C6 Bank
            "com.bradesco",                // Bradesco
            "br.com.bb.android",           // Banco do Brasil
            "com.caixa.gov.meucaixa",      // Caixa
            "br.com.picpay",                // PicPay
            "com.original.original",        // Banco Original
            "com.neon.neon",                // Neon
            "com.btg.wl.pactualwl",         // BTG+
            "com.santander.app",            // Santander
        )
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (sbn.packageName !in BANK_PACKAGES) return

        val extras = sbn.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString().orEmpty()
        val corpo = listOf(title, text, bigText).filter { it.isNotBlank() }.distinct().joinToString(" - ")
        if (corpo.isBlank()) return

        val token = Prefs.getToken(applicationContext) ?: return
        ApiClient.sendCapture(token, corpo) { ok ->
            if (ok) Prefs.setLastCapture(applicationContext, corpo)
        }
    }
}
