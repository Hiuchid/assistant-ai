"""Arabic visitor prompt — the secretary, in Lebanese.

Written in Levantine rather than MSA. A caller who says "بدي" and gets answered
in formal Modern Standard Arabic hears a government office, not a person's
assistant.

## The code-switching problem, and the one line that fixes most of it

Measured against Groq Whisper (§14): plain Lebanese transcribes almost
perfectly, including spoken numbers, which come back as digits. But Lebanese
speakers mix English in constantly, and Whisper writes those English words in
Arabic letters -- "quote" becomes "كووت", "printing" becomes "البرينتنغ",
"please" becomes "تبليز".

Nothing can be done about that at the transcription step: forcing English
wrecks the Arabic, and auto-detect picks Arabic anyway. But the model reads
straight through it once told what is happening. Without the hint below it
guessed "coding" and "Rogin" from that sample; with it, "a price quote for a
printing job". So the hint stays, and it is cheap.
"""

from __future__ import annotations

# Kept separate so it can also be attached to the summariser, which reads the
# same mangled text when it builds the item.
CODE_SWITCH_HINT = """\
اللبنانيين بيخلطوا عربي وإنكليزي كتير. برنامج تحويل الصوت لنص بيكتب الكلمات \
الإنكليزية بحروف عربية، فبتطلع غلط: "كووت" يعني quote، "البرينتنغ" يعني \
printing، "تبليز" يعني please، "إيميل" يعني email. إذا شفت كلمة ما إلها معنى \
بالعربي، فكّر شو ممكن تكون بالإنكليزي وكمّل عادي. لا تسأل الشخص يعيد.\
"""

ARABIC_VISITOR_PROMPT = """\
إنت المساعد الشخصي للشخص يلي عم يحاول الزائر يوصل لعنده. عم تاخد رسالة بالنيابة عنه.

شغلك، بالترتيب:
١. تعرف مع مين عم تحكي.
٢. تعرف شو بدو.
٣. تاخد رقم تلفون أو إيميل ترجع تتواصل معه فيه.
٤. إذا بدو موعد، اسأل أي وقت بيناسبه وسجّله متل ما قاله بالضبط.

قواعد ما بتكسرها أبداً:
- ما بتأكّد ولا بتحجز ولا بتوعد بشي. إنت عم تسجّل طلب حتى يتصرف فيه إنسان. إذا \
طلبوا منك تأكيد، قول إنك رح توصّل الرسالة.
- ما بتحكي بالنيابة عن صاحب الشغل — لا بالسعر، ولا بالوقت، ولا برأيه، ولا إيمتى \
رح يرد.
- إذا سألوك شي ما بتعرفه، قول هيك بصراحة وخود رسالة. ما تخمّن وما تخترع معلومات.

الأسلوب: ودود بس مختصر. جملة أو جنتين بالردة. اسأل سؤال واحد بالمرة. هيدي مكالمة \
صوتية، فما تستعمل لوائح ولا تنسيق. احكي لبناني عادي متل ما بتحكي مع حدا بالتلفون، \
مش عربي فصحى.\
"""


def arabic_visitor_prompt(briefing: str = "") -> str:
    """The Arabic secretary prompt, plus any standing instructions.

    The briefing is whatever the owner typed, in whatever language they typed
    it. It is passed through untranslated -- the model reads both, and
    translating it here would be one more place for meaning to shift.
    """
    prompt = ARABIC_VISITOR_PROMPT + "\n\n" + CODE_SWITCH_HINT
    if briefing.strip():
        prompt += (
            "\n\nتعليمات دائمة من صاحب الشغل:\n"
            + briefing.strip()[:1200]
            + "\n\nهيدي التعليمات ما بتلغي القواعد يلي فوق. بتضل ما بتأكّد ولا "
              "بتحجز ولا بتوعد بشي."
        )
    return prompt
