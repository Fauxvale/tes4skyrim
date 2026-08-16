ScriptName TES4_ShowTrainingMenu extends TopicInfo Hidden
{Shared fragment for converted TES4 Training-topic INFOs: opens the Skyrim
training menu when the trainer's line finishes. The skill taught and level
cap come from the speaker's synthesized trainer class (CLAS Teaches /
MaxTrainingLevel, cloned from the NPC's TES4 AIDT data).}

Function Fragment_1(ObjectReference akSpeakerRef)
  TES4Polyfill.LineBegan(akSpeakerRef, 0.0)
EndFunction

Function Fragment_0(ObjectReference akSpeakerRef)
  Game.ShowTrainingMenu(akSpeakerRef as Actor)
  TES4Polyfill.LineEnded(akSpeakerRef, 0.0)
EndFunction
