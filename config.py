from datetime import datetime
from enum import Enum
import pandas as pd


# Status class for showing what the watcher is currently doing
class WatcherStatus(Enum):
    COLLECTING_PEOPLE = 0
    COLLECTING_TWEETS = 1
    CATCHING_UP_KEYWORDS = 2
    CATCHING_UP_PEOPLE = 3
    WAITING_FOR_RATE_LIMIT = 4
    COLLECTION_FINISHED = 5


status = 0


# Timestep class for figuring out how much the watcher should collect in one go
class TimeSteps(Enum):
    NO_STEPS = 0
    MONTHS = 1
    WEEKS = 2
    DAYS = 3


start_date = datetime(2022, 2, 21)
end_date = datetime(2023, 1, 16)
time_step_size = TimeSteps.NO_STEPS

using_bearer_from_file = False
bearer = ""  # TODO: Maybe have the User be able to put this into a file somewhere, otherwise just put the option into the server

do_sentiment_analysis = False  # TODO: Set to True
do_bot_detection = True

collection_running = False
stop_collection = False

#TODO: Save these as well?
removed_people = None #pd.DataFrame({"Name": ["Mai Mercado","Pia Olsen Dyhr"], "WikidataID": ["Q12325752","Q531614"], "TwitterHandle": ["_MaiMercado","PiaOlsen"]})
added_people = None #pd.DataFrame({"Name": ["Miloš Zeman"], "WikidataID": ["Q29032"], "TwitterHandle": ["MZemanOficialni"]})
added_filters = None #{"emojis": ["🇩🇰"], "keywords": ["Leopard"], "hashtags": [], "handles": ["general_pavel"]}
removed_filters = None #{"emojis": ["🇩🇰"], "keywords": ["Leopard"], "hashtags": [], "handles": ["general_pavel"]}

catch_up_date = datetime(2022, 10, 16)

db_connection = None
db_process = None


people = pd.DataFrame()

tweetEmojis = ("🇺🇦,🇷🇺").split(",")
tweetWords = ("Zelenskyy,Zelensky,Zelenski,Putin,Ukrain,Ukraine,Ucrania,Ucraina,Ucraino,Ukrainian,Ukrajina,Russia,"
              "Russland,Rusland,Rusia,Russa,Russian,Rusk,Ruska,Rusko,ruso,rusa,Donetsk,Isjum,Ucraina,Kyiv,Kiev,"
              "Moscow,Kramatorsk,Krim,Crimea,Sevastopol,Bakhmut,Cherson,Kherson,Kakhovka,Mariupol,Kharkiv,Asow,Asov,"
              "Kreml,Kremlin,Western Media,Special Military,Schoigu,Lapin,Kadyrov,Surovikin,Donbass,Donetsk,Luhansk,"
              "Lugansk,Donbas,Nord Stream,Druschba,Jamal,Jagal,Dugin,Douguin,HIMARS").split(",")
tweetWordsAmbiguous = ("NATO,Missiles").split(",")
tweetHashtags = ("#RussiaTerroristState,#RussiaIsATerroristState,#UkraineRussianWar,#Ukraine,#Kyiv,#kyiv,#Russia,"
                 "#Moscow,#RecoveryofUkraine,#Borodyanka,#Ivankiv,#SlavaUkraini,#SlavaUkraïni,#Kreml,#Kremlin,"
                 "#ArmUkraineNow,#FreeTheLeopards,#Butscha,#RussianUkrainianWar,#UkraineRussianWar,#RussiaIsLosing,"
                 "#SpecialMilitaryOperation,#Roscosmos,#Rogozin,#Putin,#Kadyrov,#Chechen,#RussianArmy,#HeroesZ,"
                 "#Surovikin,#WarCriminalPutin,#UkraineWar,#Donbass,#Donbas,#Donetsk,#Lugansk,#SMO,#Moscow,"
                 "#UkranianAgony,#sanctions,#NordStream,#NordStreamSabotage,#Russie,#trainingmission").split(",")
tweetHandles = ("@AndriyYermak,@ZelenskyyUa,@DefenceU,@Denys_Shmyhal,@EmbEspKyiv,@DmytroKuleba,@jensstoltenberg,"
                "@KremlinRussia_E,@KremlinRussia,@mod_russia,@MelnykAndrij,@Makeiev").split(",")