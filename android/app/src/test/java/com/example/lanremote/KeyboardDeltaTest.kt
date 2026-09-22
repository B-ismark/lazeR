package com.example.lanremote

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [keyboardDelta] turns an edit of the phone's text field into what the laptop
 * must receive. The laptop's caret sits at the end of what was sent, so every
 * case is "backspace this many, then type this". [keyboardOps] is the same edit
 * as the keystrokes that carry it out.
 */
class KeyboardDeltaTest {

    @Test fun unchangedSendsNothing() =
        assertEquals(0 to "", keyboardDelta("hello", "hello"))

    @Test fun appendTypesOnlyTheNewText() =
        assertEquals(0 to "lo", keyboardDelta("hel", "hello"))

    @Test fun deleteSendsOnlyBackspaces() =
        assertEquals(2 to "", keyboardDelta("hello", "hel"))

    @Test fun clearingTheFieldDeletesEverything() =
        assertEquals(5 to "", keyboardDelta("hello", ""))

    // The regression: an autocorrect used to rewind and retype the whole field.
    @Test fun autocorrectRewritesOnlyTheChangedTail() =
        assertEquals(2 to "own", keyboardDelta("the quick brwn", "the quick brown"))

    @Test fun midFieldEditRewindsToTheEditPoint() =
        assertEquals(2 to "Xcd", keyboardDelta("abcd", "abXcd"))

    // One backspace on the laptop removes a whole emoji: two UTF-16 units here.
    @Test fun deletingAnEmojiIsOneBackspace() =
        assertEquals(1 to "", keyboardDelta("ok😀", "ok"))

    // 😀 and 😃 share a high surrogate. Keeping it as "common" would leave half an
    // emoji on the laptop and then type the other half on its own.
    @Test fun swappingEmojiNeverSplitsASurrogatePair() =
        assertEquals(1 to "😃", keyboardDelta("hi 😀", "hi 😃"))

    // --- keyboardOps: line breaks go out as Shift+Enter, never a bare Enter ---

    @Test fun lineBreakIsSentAsNewLine() =
        assertEquals(listOf(KeyOp.NewLine), keyboardOps("hi", "hi\n"))

    @Test fun textAfterALineBreakIsTypedAfterTheNewLine() =
        assertEquals(listOf(KeyOp.NewLine, KeyOp.Type("yo")), keyboardOps("hi", "hi\nyo"))

    @Test fun pastedLinesAlternateTextAndNewLines() =
        assertEquals(
            listOf(KeyOp.Type("a"), KeyOp.NewLine, KeyOp.NewLine, KeyOp.Type("b")),
            keyboardOps("", "a\n\nb"),
        )

    @Test fun backspacingOverALineBreakIsOneBackspace() =
        assertEquals(listOf(KeyOp.Backspace, KeyOp.Backspace), keyboardOps("a\nb", "a"))

    // A CRLF paste: the '\r' would otherwise be typed, and arrive as Enter.
    @Test fun pastedCrlfIsOneNewLineAndNoEnter() =
        assertEquals(
            listOf(KeyOp.Type("a"), KeyOp.NewLine, KeyOp.Type("b")),
            keyboardOps("", "a\r\nb"),
        )

    @Test fun backspacingOverACrlfIsOneBackspace() =
        assertEquals(listOf(KeyOp.Backspace, KeyOp.Backspace), keyboardOps("a\r\nb", "a"))

    // --- dropLastCodePoint: the on-screen Backspace's edit of the buffer ---

    @Test fun onScreenBackspaceDropsOneLetter() =
        assertEquals("ok", dropLastCodePoint("oka"))

    @Test fun onScreenBackspaceDropsAWholeEmoji() =
        assertEquals("ok", dropLastCodePoint("ok😀"))

    @Test fun onScreenBackspaceOnEmptyIsEmpty() =
        assertEquals("", dropLastCodePoint(""))
}
