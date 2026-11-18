-- PROPOSAL ONLY: DO NOT APPLY BEFORE SCRATCH DB + EXTERNAL META VERIFIER TESTS.
-- Owner catalog is the source of truth; the DB member snapshot is generated
-- from its locked 2,211 verse IDs. This DDL NEVER imports old publication rows.
-- Posts cannot be recorded merely from Buffer ACCEPTED, B2 upload or generation.
create table public.calm_reels_v1_catalog_members (
  ordinal integer primary key check (ordinal between 1 and 2211),
  ayah_key text not null unique check (ayah_key ~ '^[1-9][0-9]*:[1-9][0-9]*$')
);
insert into public.calm_reels_v1_catalog_members(ordinal,ayah_key)
select x.ordinality::integer,x.ayah_key
from jsonb_array_elements_text($owner$["1:1","1:3","1:4","1:5","1:6","1:7","2:2","2:3","2:4","2:5","2:20","2:25","2:35","2:37","2:38","2:43","2:44","2:45","2:52","2:53","2:56","2:57","2:58","2:60","2:64","2:70","2:73","2:74","2:82","2:97","2:103","2:107","2:110","2:112","2:115","2:123","2:124","2:125","2:127","2:128","2:129","2:130","2:132","2:133","2:134","2:136","2:138","2:139","2:141","2:142","2:143","2:149","2:150","2:151","2:153","2:155","2:156","2:157","2:158","2:160","2:163","2:164","2:169","2:172","2:177","2:182","2:184","2:185","2:186","2:187","2:192","2:195","2:198","2:199","2:200","2:202","2:207","2:208","2:213","2:214","2:215","2:218","2:219","2:222","2:223","2:224","2:225","2:226","2:228","2:229","2:231","2:233","2:235","2:236","2:237","2:238","2:239","2:242","2:243","2:245","2:248","2:249","2:255","2:259","2:260","2:261","2:262","2:263","2:265","2:266","2:267","2:268","2:270","2:272","2:273","2:274","2:277","2:278","2:280","2:281","2:283","2:284","2:285","3:2","3:6","3:8","3:14","3:15","3:17","3:18","3:25","3:26","3:27","3:30","3:31","3:35","3:36","3:37","3:38","3:41","3:43","3:47","3:51","3:53","3:57","3:60","3:62","3:68","3:73","3:74","3:76","3:79","3:81","3:84","3:89","3:92","3:96","3:102","3:104","3:107","3:108","3:114","3:120","3:128","3:129","3:132","3:133","3:134","3:135","3:136","3:138","3:139","3:142","3:145","3:148","3:150","3:159","3:160","3:161","3:163","3:164","3:170","3:171","3:172","3:173","3:174","3:175","3:179","3:184","3:190","3:194","3:198","3:200","4:2","4:4","4:5","4:6","4:8","4:9","4:13","4:16","4:17","4:19","4:21","4:23","4:26","4:27","4:28","4:32","4:34","4:35","4:38","4:39","4:40","4:43","4:45","4:49","4:54","4:57","4:58","4:64","4:65","4:67","4:68","4:69","4:70","4:79","4:83","4:85","4:86","4:87","4:94","4:95","4:96","4:98","4:99","4:100","4:103","4:104","4:106","4:107","4:110","4:111","4:113","4:114","4:120","4:122","4:124","4:125","4:126","4:128","4:129","4:130","4:132","4:134","4:146","4:148","4:149","4:152","4:158","4:162","4:165","4:166","4:174","4:175","5:1","5:4","5:6","5:7","5:9","5:11","5:15","5:16","5:20","5:23","5:25","5:34","5:39","5:40","5:42","5:46","5:55","5:56","5:62","5:63","5:74","5:75","5:79","5:83","5:84","5:85","5:88","5:91","5:93","5:97","5:100","5:101","5:104","5:105","5:108","5:111","5:112","5:113","5:114","5:117","5:118","5:119","5:120","6:3","6:17","6:32","6:34","6:35","6:46","6:48","6:51","6:52","6:53","6:54","6:57","6:58","6:62","6:63","6:68","6:72","6:75","6:77","6:82","6:83","6:84","6:86","6:87","6:90","6:91","6:92","6:95","6:96","6:97","6:98","6:99","6:101","6:102","6:103","6:104","6:105","6:112","6:115","6:116","6:117","6:118","6:123","6:125","6:126","6:127","6:129","6:131","6:132","6:133","6:135","6:141","6:142","6:149","6:153","6:154","6:155","6:156","6:158","6:162","6:164","7:2","7:10","7:17","7:19","7:22","7:23","7:27","7:29","7:30","7:32","7:42","7:43","7:46","7:49","7:52","7:54","7:55","7:56","7:57","7:58","7:63","7:68","7:69","7:72","7:74","7:75","7:83","7:87","7:89","7:95","7:96","7:99","7:113","7:114","7:120","7:121","7:122","7:125","7:126","7:128","7:140","7:144","7:146","7:149","7:151","7:153","7:155","7:157","7:158","7:159","7:160","7:161","7:169","7:170","7:174","7:178","7:180","7:181","7:184","7:185","7:186","7:189","7:193","7:196","7:198","7:199","7:200","7:203","7:204","8:2","8:3","8:4","8:27","8:28","8:33","8:40","8:46","8:47","8:51","8:53","8:56","8:58","8:61","8:62","8:64","8:66","8:71","8:74","9:11","9:15","9:16","9:18","9:19","9:21","9:22","9:24","9:27","9:44","9:51","9:59","9:60","9:71","9:72","9:75","9:89","9:91","9:92","9:99","9:100","9:102","9:103","9:104","9:105","9:106","9:108","9:112","9:114","9:115","9:116","9:117","9:118","9:119","9:121","9:124","9:126","9:128","9:129","10:3","10:5","10:6","10:9","10:10","10:11","10:12","10:21","10:22","10:24","10:25","10:26","10:30","10:31","10:32","10:41","10:43","10:44","10:46","10:47","10:55","10:57","10:58","10:59","10:60","10:62","10:63","10:64","10:65","10:67","10:72","10:73","10:84","10:85","10:87","10:89","10:100","10:103","10:104","10:107","10:108","10:109","11:1","11:5","11:6","11:10","11:11","11:14","11:15","11:23","11:28","11:29","11:40","11:41","11:43","11:47","11:49","11:51","11:52","11:56","11:61","11:62","11:63","11:66","11:69","11:71","11:73","11:74","11:88","11:90","11:108","11:111","11:112","11:114","11:115","11:120","11:122","11:123","12:2","12:18","12:19","12:20","12:22","12:29","12:30","12:31","12:32","12:33","12:34","12:39","12:49","12:51","12:52","12:53","12:54","12:55","12:56","12:57","12:64","12:65","12:67","12:77","12:78","12:80","12:83","12:84","12:86","12:88","12:90","12:92","12:97","12:98","12:104","12:109","12:111","13:3","13:4","13:11","13:12","13:22","13:23","13:24","13:26","13:28","13:29","14:1","14:4","14:5","14:10","14:11","14:12","14:23","14:25","14:27","14:31","14:32","14:36","14:37","14:39","14:40","14:41","14:51","14:52","15:20","15:22","15:28","15:33","15:45","15:46","15:47","15:48","15:49","15:52","15:54","15:55","15:56","15:59","15:67","15:75","15:77","15:85","15:87","15:92","15:97","16:2","16:5","16:7","16:9","16:10","16:11","16:12","16:13","16:14","16:15","16:16","16:18","16:23","16:30","16:31","16:32","16:33","16:36","16:37","16:41","16:42","16:44","16:48","16:51","16:53","16:58","16:59","16:61","16:64","16:65","16:66","16:67","16:69","16:71","16:75","16:78","16:79","16:80","16:81","16:89","16:90","16:93","16:96","16:97","16:99","16:102","16:103","16:110","16:111","16:114","16:115","16:119","16:121","16:122","16:124","16:125","16:126","16:127","16:128","17:1","17:3","17:6","17:9","17:11","17:12","17:15","17:17","17:19","17:20","17:21","17:23","17:24","17:25","17:26","17:28","17:29","17:30","17:34","17:36","17:38","17:40","17:44","17:48","17:53","17:54","17:55","17:66","17:70","17:71","17:73","17:74","17:78","17:79","17:80","17:82","17:83","17:84","17:86","17:87","17:88","17:91","17:93","17:95","17:100","17:105","17:109","17:110","17:111","18:1","18:2","18:3","18:6","18:9","18:10","18:13","18:14","18:16","18:17","18:18","18:19","18:21","18:24","18:28","18:30","18:33","18:39","18:43","18:44","18:46","18:57","18:59","18:62","18:63","18:64","18:65","18:67","18:68","18:69","18:70","18:71","18:72","18:73","18:75","18:76","18:77","18:79","18:81","18:84","18:85","18:86","18:88","18:89","18:90","18:92","18:93","18:94","18:95","18:96","18:97","18:98","18:107","18:108","19:2","19:3","19:4","19:5","19:9","19:10","19:13","19:14","19:15","19:17","19:18","19:20","19:21","19:24","19:26","19:29","19:31","19:32","19:33","19:35","19:36","19:42","19:43","19:46","19:47","19:48","19:50","19:53","19:55","19:60","19:61","19:62","19:63","19:76","19:78","19:84","19:85","19:87","19:91","19:92","19:93","19:96","19:97","20:2","20:3","20:5","20:8","20:14","20:15","20:17","20:18","20:23","20:25","20:26","20:27","20:28","20:29","20:31","20:33","20:34","20:35","20:36","20:38","20:39","20:44","20:45","20:46","20:50","20:54","20:62","20:68","20:70","20:72","20:73","20:75","20:76","20:79","20:82","20:84","20:90","20:98","20:108","20:109","20:111","20:112","20:113","20:117","20:118","20:119","20:121","20:122","20:123","20:126","20:128","20:129","20:130","20:131","20:132","20:133","20:135","21:4","21:10","21:11","21:13","21:15","21:21","21:22","21:25","21:26","21:32","21:34","21:37","21:42","21:43","21:47","21:48","21:50","21:51","21:56","21:64","21:66","21:67","21:70","21:71","21:73","21:75","21:76","21:78","21:80","21:81","21:83","21:84","21:85","21:86","21:89","21:90","21:92","21:102","21:103","21:105","21:106","21:107","21:108","21:112","22:5","22:8","22:14","22:15","22:16","22:23","22:24","22:28","22:34","22:35","22:36","22:37","22:41","22:50","22:54","22:56","22:59","22:60","22:62","22:63","22:65","22:67","22:69","22:70","22:78","23:1","23:2","23:3","23:4","23:8","23:9","23:10","23:11","23:14","23:19","23:21","23:23","23:25","23:26","23:27","23:28","23:29","23:32","23:34","23:39","23:47","23:49","23:50","23:52","23:58","23:60","23:61","23:62","23:70","23:72","23:73","23:75","23:78","23:85","23:91","23:93","23:94","23:96","23:97","23:98","23:102","23:109","23:111","23:116","23:118","24:5","24:10","24:12","24:15","24:16","24:20","24:21","24:22","24:25","24:26","24:31","24:32","24:33","24:35","24:37","24:38","24:43","24:44","24:46","24:51","24:54","24:56","24:61","24:62","25:1","25:2","25:6","25:8","25:9","25:10","25:15","25:16","25:20","25:24","25:31","25:45","25:47","25:48","25:49","25:54","25:56","25:57","25:58","25:59","25:60","25:61","25:62","25:63","25:67","25:70","25:71","25:72","25:74","25:76","26:3","26:5","26:7","26:9","26:13","26:15","26:20","26:24","26:41","26:42","26:46","26:47","26:48","26:50","26:51","26:62","26:65","26:68","26:72","26:77","26:78","26:82","26:83","26:84","26:85","26:86","26:87","26:88","26:90","26:104","26:107","26:109","26:113","26:117","26:118","26:119","26:122","26:125","26:127","26:132","26:133","26:134","26:140","26:143","26:145","26:146","26:147","26:148","26:154","26:159","26:162","26:164","26:169","26:170","26:175","26:178","26:180","26:186","26:191","26:205","26:207","26:208","26:209","26:215","26:217","26:227","27:1","27:2","27:3","27:7","27:9","27:11","27:12","27:15","27:16","27:18","27:19","27:26","27:30","27:35","27:36","27:39","27:44","27:46","27:53","27:57","27:70","27:73","27:77","27:78","27:79","27:80","27:81","27:86","27:89","27:92","27:93","28:5","28:7","28:10","28:13","28:14","28:16","28:17","28:21","28:22","28:24","28:25","28:26","28:34","28:37","28:43","28:44","28:46","28:47","28:49","28:50","28:52","28:53","28:54","28:55","28:56","28:59","28:60","28:67","28:70","28:73","28:77","28:83","28:85","28:88","29:2","29:5","29:9","29:15","29:19","29:21","29:27","29:30","29:33","29:35","29:36","29:44","29:45","29:51","29:58","29:59","29:60","29:62","29:64","29:69","30:5","30:7","30:15","30:17","30:20","30:21","30:22","30:23","30:24","30:27","30:29","30:30","30:36","30:37","30:38","30:39","30:43","30:46","30:47","30:48","30:49","30:50","30:52","30:53","30:60","31:3","31:4","31:5","31:8","31:9","31:14","31:17","31:19","31:22","31:31","32:3","32:6","32:9","32:12","32:15","32:16","32:17","32:18","32:19","32:24","32:25","32:26","32:27","32:30","33:3","33:4","33:5","33:17","33:21","33:22","33:23","33:27","33:29","33:31","33:35","33:37","33:42","33:43","33:44","33:45","33:46","33:47","33:50","33:56","33:58","33:59","33:70","33:71","34:2","34:4","34:6","34:9","34:10","34:13","34:15","34:18","34:24","34:26","34:32","34:36","34:37","34:39","34:47","34:50","34:54","35:2","35:3","35:8","35:9","35:11","35:12","35:19","35:20","35:21","35:22","35:27","35:28","35:29","35:30","35:32","35:33","35:34","35:35","35:41","35:42","35:45","36:4","36:5","36:10","36:11","36:15","36:20","36:21","36:22","36:25","36:26","36:27","36:33","36:34","36:35","36:37","36:42","36:43","36:44","36:45","36:54","36:55","36:56","36:57","36:58","36:65","36:68","36:69","36:71","36:72","36:73","36:76","36:80","37:2","37:4","37:39","37:40","37:41","37:42","37:43","37:44","37:45","37:46","37:47","37:48","37:49","37:50","37:51","37:58","37:60","37:61","37:74","37:75","37:76","37:77","37:79","37:80","37:81","37:87","37:99","37:100","37:101","37:102","37:105","37:107","37:109","37:110","37:111","37:112","37:113","37:114","37:115","37:118","37:120","37:121","37:122","37:124","37:126","37:128","37:130","37:131","37:132","37:134","37:145","37:146","37:148","37:159","37:160","37:174","37:178","37:180","37:181","38:1","38:9","38:17","38:18","38:22","38:24","38:25","38:28","38:29","38:30","38:32","38:35","38:39","38:40","38:42","38:43","38:44","38:45","38:49","38:50","38:51","38:52","38:53","38:54","38:65","38:66","38:71","38:86","38:87","38:88","39:2","39:5","39:6","39:9","39:10","39:11","39:12","39:14","39:17","39:18","39:20","39:21","39:23","39:27","39:28","39:33","39:34","39:37","39:41","39:44","39:46","39:52","39:53","39:57","39:62","39:66","39:69","39:70","39:73","39:74","40:8","40:9","40:13","40:16","40:17","40:27","40:31","40:32","40:33","40:38","40:40","40:44","40:51","40:54","40:55","40:56","40:61","40:64","40:65","40:77","40:79","40:80","41:2","41:3","41:8","41:10","41:18","41:30","41:31","41:32","41:33","41:34","41:35","41:36","41:37","41:39","41:42","41:46","41:49","41:51","41:53","42:5","42:8","42:10","42:11","42:12","42:15","42:17","42:19","42:20","42:22","42:23","42:25","42:27","42:28","42:29","42:30","42:32","42:33","42:36","42:38","42:40","42:43","42:47","42:51","42:52","42:53","43:2","43:9","43:10","43:11","43:12","43:13","43:14","43:27","43:28","43:29","43:32","43:36","43:37","43:40","43:41","43:42","43:43","43:44","43:45","43:49","43:61","43:62","43:63","43:64","43:67","43:68","43:69","43:70","43:71","43:72","43:73","43:76","43:82","43:84","43:85","44:3","44:5","44:6","44:7","44:8","44:18","44:20","44:21","44:27","44:32","44:33","44:42","44:51","44:52","44:53","44:54","44:55","44:57","44:58","44:59","45:3","45:4","45:5","45:12","45:13","45:14","45:15","45:17","45:20","45:22","45:23","45:29","45:30","46:8","46:12","46:13","46:14","46:15","46:16","46:19","46:26","46:27","46:29","46:30","46:35","47:5","47:6","47:7","47:17","47:19","47:25","47:31","47:35","47:36","47:37","47:38","48:1","48:2","48:4","48:8","48:9","48:10","48:14","48:18","48:19","48:20","48:27","48:28","49:2","49:3","49:4","49:5","49:8","49:10","49:12","49:14","49:15","49:16","49:17","50:6","50:7","50:8","50:9","50:10","50:11","50:27","50:28","50:29","50:31","50:32","50:33","50:34","50:35","50:37","50:39","50:44","50:45","51:3","51:4","51:9","51:15","51:16","51:17","51:18","51:19","51:20","51:21","51:22","51:25","51:26","51:28","51:34","51:35","51:36","51:38","51:48","51:49","51:50","51:54","51:55","51:57","51:58","52:17","52:19","52:20","52:21","52:22","52:23","52:24","52:25","52:28","52:29","52:31","52:35","52:45","52:48","53:15","53:16","53:18","53:30","53:31","53:32","53:38","53:39","53:41","53:55","54:10","54:15","54:17","54:22","54:24","54:26","54:27","54:28","54:32","54:34","54:35","54:40","54:54","54:55","55:1","55:2","55:4","55:5","55:6","55:7","55:8","55:9","55:10","55:11","55:12","55:13","55:16","55:18","55:19","55:20","55:21","55:22","55:23","55:24","55:25","55:28","55:29","55:30","55:32","55:34","55:36","55:38","55:40","55:42","55:45","55:46","55:47","55:48","55:49","55:50","55:51","55:52","55:53","55:54","55:55","55:56","55:57","55:58","55:59","55:60","55:61","55:62","55:63","55:64","55:65","55:66","55:67","55:68","55:69","55:70","55:71","55:72","55:73","55:74","55:75","55:76","55:77","55:78","56:8","56:10","56:12","56:13","56:14","56:15","56:16","56:17","56:18","56:19","56:20","56:21","56:22","56:23","56:24","56:25","56:26","56:27","56:28","56:29","56:30","56:31","56:32","56:33","56:34","56:35","56:36","56:37","56:38","56:39","56:40","56:58","56:63","56:68","56:70","56:72","56:73","56:75","56:77","56:88","56:89","56:90","56:91","57:1","57:7","57:8","57:9","57:11","57:12","57:17","57:18","57:21","57:22","57:23","57:24","57:26","57:27","57:28","58:9","58:10","58:11","58:12","58:13","58:22","59:1","59:8","59:9","59:10","59:21","59:22","59:24","60:3","60:6","60:7","61:1","61:2","61:5","61:11","61:12","61:13","62:1","62:2","62:3","62:4","62:5","62:9","62:10","62:11","63:10","64:1","64:8","64:11","64:13","64:14","64:15","64:16","64:17","65:3","65:4","65:7","65:11","66:1","66:4","66:11","67:1","67:2","67:3","67:4","67:12","67:15","67:19","67:22","67:23","67:29","68:3","68:6","68:7","68:17","68:24","68:28","68:29","68:30","68:32","68:34","68:36","68:48","68:49","68:52","69:12","69:21","69:22","69:23","69:24","69:34","69:38","69:48","70:5","70:19","70:20","70:22","70:23","70:24","70:25","70:26","70:32","70:34","70:35","71:4","71:5","71:7","71:10","71:11","71:12","71:16","71:19","71:28","72:1","72:3","72:10","72:13","72:14","72:16","72:18","72:19","72:22","73:2","73:3","73:9","73:10","73:19","74:6","74:7","74:11","74:12","74:13","74:14","74:38","74:39","74:40","74:54","74:55","74:56","75:19","75:20","75:22","75:23","75:39","76:5","76:6","76:8","76:9","76:11","76:12","76:13","76:14","76:15","76:16","76:17","76:18","76:19","76:20","76:21","76:22","76:25","76:27","76:29","77:1","77:2","77:3","77:23","77:25","77:27","77:41","77:42","77:43","77:44","78:6","78:7","78:8","78:9","78:10","78:12","78:13","78:14","78:16","78:31","78:32","78:33","78:34","78:35","78:36","78:37","78:38","79:19","79:26","79:29","79:32","79:33","79:41","80:4","80:11","80:12","80:18","80:20","80:24","80:25","80:26","80:32","80:38","80:39","81:13","81:27","82:13","82:19","83:18","83:22","83:23","83:24","83:25","83:26","83:27","83:28","83:35","84:8","84:9","84:25","85:8","85:11","85:14","87:1","87:3","87:8","87:10","87:15","88:8","88:9","88:10","88:11","88:12","88:13","88:14","88:15","88:16","88:17","88:18","88:19","88:20","88:26","89:3","89:4","89:5","89:14","89:15","89:16","89:18","89:20","89:27","89:28","89:29","89:30","90:8","90:9","90:10","90:14","90:16","90:17","90:18","91:8","92:4","92:5","92:6","92:7","92:12","92:17","92:18","92:19","92:20","92:21","93:3","93:4","93:5","93:6","93:7","93:8","93:11","94:1","94:2","94:3","94:5","94:6","94:7","94:8","95:1","95:6","95:8","96:3","96:5","96:11","96:12","97:3","97:5","98:5","98:7","98:8","99:7","99:8","100:1","100:4","100:5","100:8","100:10","101:7","101:8","102:8","103:3","106:1","106:2","106:3","106:4","107:5","107:6","107:7","108:1","108:2","109:2","109:3","109:4","109:5","110:2","110:3","112:1","112:2","112:3","112:4","113:1","113:2","113:3","113:5","114:1","114:2","114:3","114:6"]$owner$::jsonb)
  with ordinality as x(ayah_key,ordinality);
do $verify$
begin
  if (select count(*) from public.calm_reels_v1_catalog_members) <> 2211
     or (select ayah_key from public.calm_reels_v1_catalog_members where ordinal=1) <> '1:1' then
    raise exception 'Frozen 2,211-member owner list failed verification';
  end if;
end $verify$;

-- Immutable-in-purpose dispatch claim: only one unconfirmed social dispatch
-- per cycle may exist. A timeout/unknown outcome intentionally blocks retries
-- until the operator performs independent provider reconciliation.
create table public.calm_reels_v1_dispatch_claims (
  cycle_number integer not null check (cycle_number >= 1),
  ayah_key text not null references public.calm_reels_v1_catalog_members(ayah_key),
  run_id text not null check (length(run_id) between 1 and 120),
  last_ayah_key text not null references public.calm_reels_v1_catalog_members(ayah_key),
  state text not null default 'CLAIMED' check (state in ('CLAIMED','CONFIRMED')),
  claimed_at timestamptz not null default now(),
  confirmed_at timestamptz,
  primary key (cycle_number,ayah_key),
  unique (cycle_number,run_id),
  check ((state='CLAIMED' and confirmed_at is null)
      or (state='CONFIRMED' and confirmed_at is not null))
);
create unique index calm_reels_v1_one_pending_dispatch_per_cycle
  on public.calm_reels_v1_dispatch_claims(cycle_number)
  where state='CLAIMED';
alter table public.calm_reels_v1_dispatch_claims enable row level security;
revoke all on public.calm_reels_v1_dispatch_claims
  from public,anon,authenticated;
grant select,insert,update on public.calm_reels_v1_dispatch_claims to service_role;

-- This RPC must finish BEFORE initiating any externally visible dispatch.
-- Never automatically reclaim a timeout: unknown outcomes may already be live.
create function public.calm_reels_v1_claim_next_ayah(
  p_cycle_number integer, p_ayah_key text, p_run_id text
) returns jsonb
language plpgsql
security invoker
set search_path = ''
as $claim$
declare
  current_cycle public.calm_reels_v1_cycle_state%rowtype;
  next_key text;
  outstanding public.calm_reels_v1_dispatch_claims%rowtype;
begin
  select * into current_cycle
  from public.calm_reels_v1_cycle_state
  where singleton=true for update;
  if not found or current_cycle.catalog_sha256 <>
    'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'
    or current_cycle.total_ayahs <> 2211
    or p_cycle_number is distinct from current_cycle.cycle_number
    or p_run_id is null or length(p_run_id) not between 1 and 120 then
    raise exception 'Invalid v1 claim identity or frozen cycle';
  end if;
  select m.ayah_key into next_key
  from public.calm_reels_v1_catalog_members m
  where not exists (
    select 1 from public.calm_reels_v1_confirmed_posts receipt
    where receipt.cycle_number=current_cycle.cycle_number
      and receipt.ayah_key=m.ayah_key and receipt.platform='facebook'
  ) order by m.ordinal limit 1;
  if next_key is null or p_ayah_key is distinct from next_key then
    raise exception 'Only first pending owner ayah may be claimed';
  end if;
  select * into outstanding
  from public.calm_reels_v1_dispatch_claims c
  where c.cycle_number=current_cycle.cycle_number and c.state='CLAIMED';
  if found then
    if outstanding.run_id=p_run_id and outstanding.ayah_key=p_ayah_key then
      return pg_catalog.jsonb_build_object('state','ALREADY_CLAIMED',
        'ayah_key',next_key,'last_ayah_key',outstanding.last_ayah_key,'cycle_number',current_cycle.cycle_number);
    end if;
    raise exception 'Pending dispatch requires provider reconciliation before retry';
  end if;
  declare
    saved jsonb; end_key text := next_key;
  begin
    select r.value into saved from public.run_records r where r.run_id=p_run_id for share;
    if saved is not null then
      if next_key is distinct from ((saved->>'surah_number')||':'||(saved->>'first_ayah')) then
        raise exception 'Saved run does not start at the reserved ayah';
      end if;
      end_key := (saved->>'surah_number')||':'||(saved->>'last_ayah');
      if end_key is null or (end_key<>next_key and (
          (saved->>'last_ayah')::int <> (saved->>'first_ayah')::int+1
          or saved#>'{provenance,owner_v1,ayah_keys}' is distinct from pg_catalog.jsonb_build_array(next_key,end_key)
          or not exists (select 1 from public.calm_reels_v1_catalog_members m where m.ayah_key=end_key)
          or exists (select 1 from public.calm_reels_v1_confirmed_posts p where p.cycle_number=p_cycle_number and p.ayah_key=end_key)
      )) then raise exception 'Second ayah is not an eligible pending neighbour'; end if;
    end if;
    insert into public.calm_reels_v1_dispatch_claims (cycle_number,ayah_key,last_ayah_key,run_id)
      values (current_cycle.cycle_number,next_key,end_key,p_run_id);
    return pg_catalog.jsonb_build_object('state','CLAIMED','ayah_key',next_key,
      'last_ayah_key',end_key,'cycle_number',current_cycle.cycle_number);
  end;
end;
$claim$;
revoke all on function public.calm_reels_v1_claim_next_ayah(integer,text,text)
  from public,anon,authenticated;
grant execute on function public.calm_reels_v1_claim_next_ayah(integer,text,text)
  to service_role;

create table public.calm_reels_v1_completed_cycles (
  cycle_number integer primary key check (cycle_number >= 1),
  catalog_sha256 text not null check (catalog_sha256 =
    'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'),
  confirmed_ayahs integer not null check (confirmed_ayahs=2211),
  completed_at timestamptz not null default now()
);

alter table public.calm_reels_v1_catalog_members enable row level security;
alter table public.calm_reels_v1_completed_cycles enable row level security;
revoke all on public.calm_reels_v1_catalog_members,
  public.calm_reels_v1_completed_cycles from public,anon,authenticated;
grant select on public.calm_reels_v1_catalog_members to service_role;
grant select,insert on public.calm_reels_v1_completed_cycles to service_role;

-- Only the trusted server may call this RPC AFTER separately confirming
-- actual primary-platform delivery through the authenticated Meta API.
-- The DB can inspect the saved run but cannot itself prove external existence.
-- This function is never called for Buffer accepted/unknown/draft statuses.
create function public.calm_reels_v1_record_verified_facebook_post(
  p_cycle_number integer,
  p_run_id text,
  p_facebook_post_id text,
  p_platform_evidence_sha256 text,
  p_verified_at timestamptz
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $receipt$
declare
  current_cycle public.calm_reels_v1_cycle_state%rowtype;
  run_record public.run_records%rowtype;
  surah_n integer;
  first_n integer;
  last_n integer;
  ayah_keys text[];
  first_missing text;
  already_count integer;
  confirmed_count integer;
  active_claim public.calm_reels_v1_dispatch_claims%rowtype;
begin
  select * into current_cycle
  from public.calm_reels_v1_cycle_state
  where singleton=true for update;
  if not found or current_cycle.catalog_sha256 <>
    'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'
    or current_cycle.total_ayahs <> 2211 then
    raise exception 'Verified fresh catalog unavailable';
  end if;
  if p_cycle_number is distinct from current_cycle.cycle_number
    or p_run_id is null or length(p_run_id)=0
    or p_facebook_post_id is null
    or p_facebook_post_id !~ '^[1-9][0-9]{5,24}$'
    or p_platform_evidence_sha256 is null
    or p_platform_evidence_sha256 !~ '^[a-f0-9]{64}$'
    or p_verified_at is null
    or p_verified_at < current_cycle.started_at
    or p_verified_at > now() + interval '5 minutes' then
    raise exception 'Invalid or stale primary-platform receipt';
  end if;

  select * into run_record from public.run_records where run_id=p_run_id;
  if not found or run_record.run_kind not in ('scheduled','manual_production')
    or run_record.status is distinct from 'SUCCESS'
    or run_record.final_safety_passed is distinct from true
    or run_record.media_key is null
    or length(run_record.media_key)=0
    or run_record.created_at is null
    or run_record.created_at < current_cycle.started_at
    or run_record.completed_at is null
    or run_record.completed_at < current_cycle.started_at
    or run_record.value->>'facebook_status' is distinct from 'SUCCESS'
    or run_record.value->>'facebook_media_id' is distinct from p_facebook_post_id
    or run_record.value->>'buffer_post_status' is distinct from 'sent'
    or run_record.value->>'buffer_post_id' is null
    or run_record.value->>'facebook_reel_url' is null
    or run_record.value->>'media_key' is distinct from run_record.media_key
    or run_record.value->>'run_id' is distinct from p_run_id then
    raise exception 'Publication is not an independently verified current-cycle successful Reel';
  end if;

  surah_n := (run_record.value->>'surah_number')::integer;
  first_n := (run_record.value->>'first_ayah')::integer;
  last_n := (run_record.value->>'last_ayah')::integer;
  if surah_n is null or first_n is null or last_n is null
    or surah_n not between 1 and 114 or first_n < 1
    or last_n not between first_n and first_n+1 then
    raise exception 'Owner-v1 requires one ayah or an adjacent pair';
  end if;
  if not exists (select 1 from public.calm_reels_v1_dispatch_claims c
     where c.cycle_number=p_cycle_number and c.run_id=p_run_id
       and c.ayah_key=(surah_n::text||':'||first_n::text)
       and c.last_ayah_key=(surah_n::text||':'||last_n::text)) then
    raise exception 'Receipt range does not match reserved passage';
  end if;
  if last_n<>first_n and (run_record.value#>'{provenance,owner_v1,ayah_keys}' is distinct from
      pg_catalog.jsonb_build_array(surah_n::text||':'||first_n::text,surah_n::text||':'||last_n::text)) then
    raise exception 'Pair source identity changed';
  end if;
  if run_record.value->'provenance'->'owner_v1'->>'catalog_id'
       is distinct from 'calm-reels-editorial-shortlist-v1'
     or run_record.value->'provenance'->'owner_v1'->>'ayah_key'
       is distinct from (surah_n::text||':'||first_n::text)
     or run_record.value->'provenance'->'owner_v1'->>'cycle_number'
       is distinct from current_cycle.cycle_number::text
     or run_record.value->'provenance'->'owner_v1'->>'editorial_selection_only'
       is distinct from 'true'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'schema'
       is distinct from 'owner-v1-facebook-verified-delivery-v1'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'facebook_video_id'
       is distinct from p_facebook_post_id
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'evidence_sha256'
       is distinct from p_platform_evidence_sha256
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'buffer_post_id'
       is distinct from run_record.value->>'buffer_post_id'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'buffer_post_status'
       is distinct from 'sent'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'permalink_url'
       is distinct from run_record.value->>'facebook_reel_url'
     or (run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'verified_at')::timestamptz
       is distinct from p_verified_at
     or (run_record.value->>'facebook_published_at')::timestamptz
       is distinct from p_verified_at then
    raise exception 'External Facebook proof is not bound to the exact owner run';
  end if;

  select array_agg(surah_n::text||':'||g.n::text order by g.n)
  into ayah_keys
  from pg_catalog.generate_series(first_n,last_n) as g(n);
  if ayah_keys is null or exists (
    select 1 from pg_catalog.unnest(ayah_keys) as x(key)
    where not exists (
      select 1 from public.calm_reels_v1_catalog_members m where m.ayah_key=x.key
    )
  ) then
    raise exception 'Reel range crosses the owner frozen list';
  end if;

  -- Idempotent repeat of an EXACT already-recorded complete Reel is allowed.
  select count(*) into already_count
  from public.calm_reels_v1_confirmed_posts existing
  where existing.cycle_number=current_cycle.cycle_number
    and existing.ayah_key = any(ayah_keys)
    and existing.platform='facebook'
    and existing.run_id=p_run_id
    and existing.external_post_id=p_facebook_post_id
    and existing.media_key=run_record.media_key
    and existing.evidence_sha256=p_platform_evidence_sha256;
  if already_count=pg_catalog.cardinality(ayah_keys) then
    return pg_catalog.jsonb_build_object(
      'state','ALREADY_RECORDED','cycle_number',current_cycle.cycle_number,
      'confirmed_ayahs',(select count(*) from public.calm_reels_v1_confirmed_posts
        where cycle_number=current_cycle.cycle_number)
    );
  end if;
  if already_count<>0 then
    raise exception 'Partial prior delivery evidence requires manual reconciliation';
  end if;

  select * into active_claim
  from public.calm_reels_v1_dispatch_claims c
  where c.cycle_number=current_cycle.cycle_number
    and c.ayah_key=ayah_keys[1]
    and c.run_id=p_run_id and c.state='CLAIMED';
  if not found then
    raise exception 'Facebook receipt requires matching pre-dispatch claim';
  end if;

  select m.ayah_key into first_missing
  from public.calm_reels_v1_catalog_members m
  where not exists (
    select 1 from public.calm_reels_v1_confirmed_posts posted
    where posted.cycle_number=current_cycle.cycle_number
      and posted.ayah_key=m.ayah_key and posted.platform='facebook'
  )
  order by m.ordinal limit 1;
  if first_missing is distinct from ayah_keys[1] then
    raise exception 'Out-of-order or duplicate Reel: first missing ayah mismatch';
  end if;

  -- All ayahs of one exact approved Reel are committed atomically.
  insert into public.calm_reels_v1_confirmed_posts
    (cycle_number,ayah_key,run_id,media_key,platform,external_post_id,
     evidence_sha256,verified_at)
  select current_cycle.cycle_number,x.key,p_run_id,run_record.media_key,
         'facebook',p_facebook_post_id,p_platform_evidence_sha256,p_verified_at
  from pg_catalog.unnest(ayah_keys) as x(key);

  update public.calm_reels_v1_dispatch_claims
    set state='CONFIRMED',confirmed_at=p_verified_at
  where cycle_number=current_cycle.cycle_number
    and ayah_key=ayah_keys[1] and run_id=p_run_id and state='CLAIMED';
  if not found then
    raise exception 'Dispatch claim transition failed';
  end if;

  select count(*) into confirmed_count
  from public.calm_reels_v1_confirmed_posts
  where cycle_number=current_cycle.cycle_number and platform='facebook';
  if confirmed_count>2211 then
    raise exception 'More verified posts than owner catalogue';
  end if;
  if confirmed_count=2211 then
    -- Atomic rollover; preserves previous-cycle receipts and every video.
    insert into public.calm_reels_v1_completed_cycles
      (cycle_number,catalog_sha256,confirmed_ayahs)
    values (current_cycle.cycle_number,current_cycle.catalog_sha256,2211);
    update public.calm_reels_v1_cycle_state
      set cycle_number=current_cycle.cycle_number+1,started_at=now()
      where singleton=true;
    return pg_catalog.jsonb_build_object(
      'state','ROLLED_OVER','cycle_number',current_cycle.cycle_number+1,
      'confirmed_ayahs',0,'total_ayahs',2211);
  end if;
  return pg_catalog.jsonb_build_object(
    'state','RECORDED','cycle_number',current_cycle.cycle_number,
    'confirmed_ayahs',confirmed_count,'total_ayahs',2211);
end;
$receipt$;

revoke all on function public.calm_reels_v1_record_verified_facebook_post(
  integer,text,text,text,timestamptz) from public,anon,authenticated;
grant execute on function public.calm_reels_v1_record_verified_facebook_post(
  integer,text,text,text,timestamptz) to service_role;
