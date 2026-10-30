package dev.ophelia.extractor

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.zip.ZipInputStream

class BundleTest {
    @get:Rule
    val tmp = TemporaryFolder()

    @Test
    fun fileNamesAreSafeAndSayWhatTheyAre() {
        assertEquals("Balatro_com.playstack.balatro.android_1.0.1o.apks",
            Bundle.fileName("Balatro", "com.playstack.balatro.android", "1.0.1o", bundle = true))
        assertEquals("My_Game_com.x.y.apk", Bundle.fileName("My: Game/", "com.x.y", null, bundle = false))
        assertEquals("Pokémon_GO_com.nianticlabs.pokemongo_0.3.apk",
            Bundle.fileName("Pokémon GO", "com.nianticlabs.pokemongo", "0.3", bundle = false))
        assertTrue(Bundle.fileName("x".repeat(500), "com.a.b", "1", true).length <= 125)
    }

    @Test
    fun infoJsonEscapes() {
        val json = Bundle.infoJson(linkedMapOf(
            "label" to "Say \"hi\"\\\n", "code" to 42L, "files" to listOf("base.apk", "split_a.apk"),
            "none" to null,
        ))
        assertEquals(
            "{\n  \"label\": \"Say \\\"hi\\\"\\\\\\n\",\n  \"code\": 42,\n" +
                "  \"files\": [\"base.apk\", \"split_a.apk\"],\n  \"none\": null\n}\n",
            json,
        )
    }

    @Test
    fun apksBundleHoldsBaseSplitsAndInfo() {
        val base = tmp.newFile("base.apk").apply { writeBytes(ByteArray(200_000) { (it % 251).toByte() }) }
        val split = tmp.newFile("split_config.arm64_v8a.apk").apply { writeBytes(byteArrayOf(1, 2, 3)) }
        val out = ByteArrayOutputStream()
        var reported = 0L
        assertTrue(Bundle.writeApks(Bundle.sources(base, listOf(split)), "{}", out, { reported += it }))
        assertEquals(base.length() + split.length(), reported)

        val entries = mutableMapOf<String, ByteArray>()
        ZipInputStream(ByteArrayInputStream(out.toByteArray())).use { z ->
            generateSequence { z.nextEntry }.forEach { entries[it.name] = z.readBytes() }
        }
        assertEquals(listOf("base.apk", "split_config.arm64_v8a.apk", "info.json"), entries.keys.toList())
        assertTrue(entries["base.apk"]!!.contentEquals(base.readBytes()))
        assertEquals("{}", String(entries["info.json"]!!))
        // Leave a sample next to the build for the cross-check with `ophelia apk import`.
        System.getProperty("bundleSampleDir")?.let { File(it, "sample.apks").writeBytes(out.toByteArray()) }
    }

    @Test
    fun copyStopsWhenCancelled() {
        val input = ByteArrayInputStream(ByteArray(1 shl 20))
        var calls = 0
        assertFalse(Bundle.copy(input, ByteArrayOutputStream(), {}, { calls++ > 2 }))
    }
}
