from app.core.domain_detection import score_message_semantic

messages = [
    "Since she passed, I can't stop worrying about others",
    "mujhe uski yaad aati hai",
    "I have a huge exam tomorrow and I can't focus",
    "I keep having flashbacks about the accident",
    "Aaj bohot acha din tha, kaafi kaam khatam kiya",
]

for msg in messages:
    scores = score_message_semantic(msg)
    print(f'\n"{msg}"')
    for domain, score in sorted(scores.items(), key=lambda x: -x[1]):
        print(f"   {domain:15} {score:.3f}")