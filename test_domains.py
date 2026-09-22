from app.core.domain_detection import select_domains

test_cases = [
    "Since she passed, I can't stop worrying about others",
    "mujhe uski yaad aati hai",
    "I have a huge exam tomorrow and I can't focus",
    "I keep having flashbacks about the accident",
    "Aaj bohot acha din tha, kaafi kaam khatam kiya",
]

for text in test_cases:
    result = select_domains(text)
    print(f"{result} ^<- \"{text}\"")
