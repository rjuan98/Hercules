package com.hercules.companion

import android.content.Context

object Prefs {
    private const val FILE = "herc_prefs"
    private const val KEY_TOKEN = "token"
    private const val KEY_NOME = "nome"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun getToken(ctx: Context): String? = prefs(ctx).getString(KEY_TOKEN, null)
    fun getNome(ctx: Context): String? = prefs(ctx).getString(KEY_NOME, null)

    fun saveLogin(ctx: Context, token: String, nome: String?) {
        prefs(ctx).edit().putString(KEY_TOKEN, token).putString(KEY_NOME, nome).apply()
    }

    fun clear(ctx: Context) {
        prefs(ctx).edit().clear().apply()
    }
}
