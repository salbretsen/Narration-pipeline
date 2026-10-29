"""Representative manuscripts for the narrative tests (multiple characters, unattributed dialogue,
changing emotions, scene transitions, recurring characters with changing names)."""
from __future__ import annotations

from studio.parsing import Block


def P(text):
    return Block("paragraph", text)


SCENE = Block("scene_break", "")

CH1 = [
    P("The rain had not stopped for three days, and the old house on Harrow Lane smelled of damp wool and woodsmoke."),
    P("Elena Marsh stood at the kitchen window with her arms folded. Behind her, Dr. Hale set down his bag."),
    P("“You should not have come back,” Elena said quietly."),
    P("“I had no choice,” said Dr. Hale. “The letter was clear.”"),
    P("“The letter was a forgery.”"),
    P("“Then who wrote it?”"),
    P("“I don’t know.” Her voice cracked, and for a moment she could not go on."),
    P("“We will find out,” Hale said gently, “but not tonight.”"),
    SCENE,
    P("By morning the lane was a river. Tom Briggs arrived on a bicycle, soaked to the skin, shouting before he had even reached the gate."),
    P("“Mrs. Marsh! Mrs. Marsh, they have found him!” Tom yelled."),
    P("Elena’s hands began to shake. “Found who?” she whispered."),
    P("“Your husband. They found him in the river, down by the mill, and he is alive!”"),
    P("She did not remember sitting down. The kitchen floor was cold, and somewhere above her the clock was ticking too loudly, and Hale was kneeling beside her saying her name over and over."),
]

CH2 = [
    P("Three weeks later the funeral bell rang for a man nobody in the village had liked."),
    P("“I was not going to come,” Elena told the doctor. “But it felt wrong to stay away.”"),
    P("“No one would have blamed you.”"),
    P("“I would have blamed me.” She looked at the coffin, and her voice dropped to almost nothing. “He was my husband, Hale. Whatever else he was, he was that."
      "”"),
    P("Hale said nothing. Tom Briggs, standing a little apart with his cap in his hands, cleared his throat."),
    P("“Mrs. Marsh,” he began, “I am so sorry.”"),
]

BOOK = [("The Flood", CH1), ("The Funeral", CH2)]

TXT = (
    "The Flood\n\nChapter 1\n\n"
    + "\n\n".join(b.text if b.kind != "scene_break" else "* * *" for b in CH1)
    + "\n\nChapter 2\n\n"
    + "\n\n".join(b.text for b in CH2)
    + "\n"
)
