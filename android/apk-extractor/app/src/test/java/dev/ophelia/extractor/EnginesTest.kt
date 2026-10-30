package dev.ophelia.extractor

import org.junit.Assert.assertEquals
import org.junit.Test

class EnginesTest {
    @Test
    fun recognisesEnginesFromTheirFiles() {
        assertEquals(listOf("Unity (IL2CPP)"), Engines.detect(listOf(
            "lib/arm64-v8a/libil2cpp.so", "lib/arm64-v8a/libunity.so",
            "assets/bin/Data/Managed/Metadata/global-metadata.dat",
        )))
        assertEquals(listOf("LÖVE (Lua source inside)"),
            Engines.detect(listOf("lib/arm64-v8a/liblove.so", "assets/game.love")))
        assertEquals(listOf("Flutter"), Engines.detect(listOf("lib/arm64-v8a/libflutter.so")))
        assertEquals(listOf("Godot"), Engines.detect(listOf("assets/game.pck")))
        assertEquals(emptyList<String>(), Engines.detect(listOf("classes.dex", "res/layout/a.xml")))
    }

    @Test
    fun listsAbis() {
        assertEquals(listOf("arm64-v8a", "armeabi-v7a"), Engines.abis(listOf(
            "lib/armeabi-v7a/libx.so", "lib/arm64-v8a/libx.so", "lib/arm64-v8a/liby.so", "classes.dex",
        )))
    }
}
