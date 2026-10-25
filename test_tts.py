import asyncio
import edge_tts


async def test_suara():
    communicate = edge_tts.Communicate(
        text="Kucing suka masuk ke kardus karena merasa aman.",
        voice="id-ID-GadisNeural",
    )
    await communicate.save("s1.wav")


asyncio.run(test_suara())
