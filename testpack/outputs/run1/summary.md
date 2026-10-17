# Batch run summary

- API: http://localhost:8000
- Inputs: 23 | succeeded: 19 | failed / rejected / other: 4
- Narration total (succeeded): 833.7 s
- Turnaround of succeeded jobs: mean 252.5 s, max 1585.5 s

| input | job_id | status | error_code | narration_seconds | turnaround_seconds | scenes | warnings_count |
|---|---|---|---|---|---|---|---|
| core01_en_saving_tips_myvoice | 2eea2c49161e40e3b2437e002956ef83 | succeeded |  | 24.875 | 92.77 | 4 | 1 |
| core02_en_bakery_story_myvoice | b0bcc3d4915e4373b7a920d5d35fc162 | succeeded |  | 19.861 | 78.69 | 4 | 0 |
| core03_ta_screen_time_myvoice | d4f43327f85b478e96a9f8e32c14c43e | succeeded |  | 28.331 | 211.23 | 6 | 1 |
| core04_tutorial_cold_brew | b0377e8e48fa4540a3e3bb987534f390 | succeeded |  | 36.375 | 235.33 | 9 | 0 |
| core05_news_summary | b84bb7185bf84e50822079a4169644a3 | succeeded |  | 36.525 | 138.8 | 7 | 2 |
| core06_product_promo | a62288037a4f4b5bb95f5b01325c3e0a | succeeded |  | 25.45 | 187.06 | 6 | 0 |
| core07_story_lighthouse | f04ac333b96e449f98f6fa9e7302e10d | succeeded |  | 46.625 | 283.46 | 12 | 1 |
| core08_motivational_talk | 1405dec809ff420d8bd39a351e7c0368 | succeeded |  | 37.2 | 223.21 | 10 | 0 |
| core09_explainer_solar | 2c119490d9624cc0bad8036d938d5abe | succeeded |  | 87.35 | 415.87 | 17 | 0 |
| core10_scene_list_incubrix | c74fbe9be9cc4b6bbad8286375115502 | succeeded |  | 23.85 | 162.82 | 6 | 1 |
| core11_hi_tts_saving | 36c9f35f0ad842b981478dc447ba29e2 | succeeded |  | 18.45 | 126.86 | 4 | 1 |
| core12_hi_tts_monsoon | e03bcded93ac46f68ede9307264eecee | succeeded |  | 22.7 | 175.07 | 4 | 0 |
| core13_hi_script_tea | 2041588a3c054eb6870cefa999453b36 | succeeded |  | 21.925 | 211.27 | 5 | 0 |
| hard01_abstract_and_statistics | 4e6841f0d5f6495580a4ff785d8626bd | succeeded |  | 19.25 | 102.7 | 6 | 0 |
| hard02_named_person_and_brand | 328aacac8eba4e13bf7ecdaeb6b7e861 | succeeded |  | 19.525 | 90.67 | 4 | 6 |
| hard03_double_meanings | 45cb9e08605f4cc49c45349786ae3e4f | succeeded |  | 30.175 | 211.24 | 9 | 1 |
| hard04_sensitive_news | 0583890289ae42d6bf5584d80b575945 | succeeded |  | 26.15 | 150.91 | 6 | 0 |
| hard05_crop_subject_at_edge_9x16 | a2d1c52631c2476dbf1480efc97e9b07 | succeeded |  | 15.575 | 114.83 | 4 | 1 |
| hard06_five_minute_voiceover | 1ed7b2d0e7b04eeca11d75beb46229f2 | succeeded |  | 293.5 | 1585.46 | 61 | 4 |
| hard07_silent_audio | 69f6b740db9b479db68b4ddea8469cd7 | failed | NO_SPEECH_DETECTED |  | 2.19 |  | 0 |
| hard08_music_only_chords | eb199c3009f246a498b1bbd1dff9612b | failed | MUSIC_ONLY_AUDIO |  | 9.7 |  | 0 |
| hard09_wrong_type_text_renamed |  | rejected | UNSUPPORTED_FILE_TYPE |  |  |  | 0 |
| hard10_empty_script |  | rejected | EMPTY_SCRIPT |  |  |  | 0 |
