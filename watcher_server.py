import os
import re
import threading
import time

import requests
import tweepy
from flask import Flask, request, flash, redirect, url_for
from wtforms.validators import DataRequired, Length, Regexp, InputRequired
from wtforms.fields import StringField, DateField, SubmitField, SelectField
from flask_wtf import FlaskForm, CSRFProtect
from flask_wtf.file import FileField

from flask import render_template

from datetime import datetime
import pandas as pd

import twitter_watcher
import config
from flask_bootstrap import Bootstrap5, SwitchField

app = Flask(__name__)
app.secret_key = 'uahrgp98q3ztU9uwgp9JSg0upghEaOJ'
bootstrap = Bootstrap5(app)

app.config['BOOTSTRAP_BTN_STYLE'] = 'primary'

collection_paused = False
x = None


class StartCollectionParametersForm(FlaskForm):
    token_switch_field = SwitchField("Use bearer token from file",
                                     render_kw={"id": "token-switch-field", "onchange": "token_switching(this)"},
                                     false_values=[False, 'false', 'False'], default=True)
    token_field = StringField('Bearer Token for the Twitter API')

    people_field = FileField('People CSV', validators=[InputRequired()])  # , Regexp('.+\.csv$')])
    people_separator_field = StringField('Separator used', validators=[Regexp('^.{1}$')], default=",")

    filter_words_field = StringField('Filter Keywords', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    filter_emojis_field = StringField('Filter Emojis', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    filter_hashtags_field = StringField('Filter Hashtags', validators=[Regexp('^(((#[^@#,]+),)*#[^@#,]+){0,1}$')])
    filter_mentions_field = StringField('Filter Mentions', validators=[Regexp('^(((@[^@#,]+),)*@[^@#,]+){0,1}$')])

    start_date_field = DateField(format=["%Y-%m-%d"])
    end_date_field = DateField(format=["%Y-%m-%d"])
    no_end_date_field = SwitchField("No end-date",
                                    render_kw={"id": "end-date-switch-field", "onchange": "end_date_switching(this)"},
                                    false_values=[False, 'false', 'False'])
    steps_field = SelectField('Collection Steps',
                              choices=[(0, 'No Time Steps'), (1, 'Month-Wise Collection'),
                                       (2, 'Week-Wise Collection'), (3, 'Day-Wise Collection')],
                              coerce=int)

    bot_detection_field = SwitchField("Bot Detection", false_values=[False, 'false', 'False'], default=True)
    sentiment_analysis_field = SwitchField("Sentiment Analysis", false_values=[False, 'false', 'False'],
                                           default=False)  # TODO: Change to True default

    # username = StringField('Username', validators=[DataRequired(), Length(1, 20)])
    submit_field = SubmitField("Set Parameters")
    start_field = SubmitField("Start New Collection")
    start_savepoint_field = SubmitField("Resume from Savepoint")


class EditCollectionParametersForm(FlaskForm):
    pause_field = SubmitField("Pause Collection")
    stop_field = SubmitField("Stop Collection")

    add_people_field = FileField(
        'Add People from CSV')  # , validators=[InputRequired(),Regexp('.+\.csv$')])  # add your class
    add_people_separator_field = StringField('Separator used', validators=[Regexp('^.{1}$')], default=",")
    remove_people_field = FileField(
        'Remove People from CSV')  # , validators=[InputRequired(),Regexp('.+\.csv$')])  # add your class
    remove_people_separator_field = StringField('Separator used', validators=[Regexp('^.{1}$')], default=",")

    add_words_field = StringField('Add Filter Keywords', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    add_emojis_field = StringField('Add Filter Emojis', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    add_hashtags_field = StringField('Add Filter Hashtags', validators=[Regexp('^(((#[^@#,]+),)*#[^@#,]+){0,1}$')])
    add_mentions_field = StringField('Add Filter Mentions', validators=[Regexp('^(((@[^@#,]+),)*@[^@#,]+){0,1}$')])
    remove_words_field = StringField('Remove Filter Keywords', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    remove_emojis_field = StringField('Remove Filter Emojis', validators=[Regexp('^(([^@#,]+,)*[^@#,]+){0,1}$')])
    remove_hashtags_field = StringField('Remove Filter Hashtags',
                                        validators=[Regexp('^(((#[^@#,]+),)*#[^@#,]+){0,1}$')])
    remove_mentions_field = StringField('Remove Filter Mentions',
                                        validators=[Regexp('^(((@[^@#,]+),)*@[^@#,]+){0,1}$')])

    bot_detection_field = SwitchField("Do Bot Detection", false_values=[False, 'false', 'False'],
                                      default=config.do_bot_detection)
    sentiment_analysis_field = SwitchField("Do Sentiment Analysis", false_values=[False, 'false', 'False'],
                                           default=config.do_sentiment_analysis)  # TODO: Change to True default

    # start_date_field = DateField(format=["%Y-%m-%d"])
    # end_date_field = DateField(format=["%Y-%m-%d"])
    # no_end_date_field = SwitchField("No end-date", render_kw={"onchange": "end_date_switching(this)"},false_values=[False,'false','False'])

    submit_field = SubmitField("Set Changes")


# TODO: Cope with excel files, too
def read_file_data(file_data, sep):
    if file_data.endswith(".xslx") or file_data.endswith(".XSLX"):
        data = pd.read_excel(file_data, names=["Name", "WikidataID", "TwitterHandle"])
    else:
        data = pd.read_csv(file_data, sep=sep, names=["Name", "WikidataID", "TwitterHandle"])

    # Try to remove column index row if present, guessing an index row by checking cell formats
    wikidata_format = re.compile("^Q[0-9]+$")
    twitter_format = re.compile("^([A-Z]|[a-z]|[0-9]|_)+$")
    if not wikidata_format.match(data['WikidataID'].loc[data.index[0]]) or not twitter_format.match(
            data['TwitterHandle'].loc[data.index[0]]):
        data.drop(0, inplace=True)

    data.drop_duplicates(inplace=True, ignore_index=True)

    return data


def set_bearer_token(get_from_file=True, value=""):
    if get_from_file:
        config.using_bearer_from_file = True
    else:
        if value == "":
            raise Exception("Got empty bearer token")
        config.bearer = value

    # Try to verify if bearer token is valid
    t_client = tweepy.Client(bearer_token=config.bearer, return_type=tweepy.Response, wait_on_rate_limit=False)
    try:
        t_client.get_user(username="TwitterDev")
    except tweepy.errors.TooManyRequests or tweepy.errors.TwitterServerError:
        flash(
            "Could not verify bearer token, rate limit was hit. Either wait 15 minutes or start collection anyways but the token may not work")


# TODO: Make alert dismissing work correctly
# TODO: Maybe display at which step the collection is currently at via string
@app.route('/', methods=('GET', 'POST'))
@app.route('/TwitterWatcher', methods=('GET', 'POST'))
def index():
    global collection_paused, x
    # config.collection_running = True  # TODO: Remove

    if not config.collection_running and not collection_paused:
        button_form = StartCollectionParametersForm()  # start_date_field=datetime.datetime.today().date(), end_date_field=datetime.datetime.today().date())
        button_form.start_field.render_kw = {'disabled': 'disabled'}

        # Check if savepoint exists
        if not (os.path.isfile("./savepoint/savepoint.json") and os.path.isfile("./savepoint/people.csv")):
            button_form.start_savepoint_field.render_kw = {'disabled': 'disabled'}

        if button_form.start_date_field.data is None:
            button_form.start_date_field.render_kw = {"value": datetime.today().date()}  # + ""}
        if button_form.end_date_field.data is None:
            button_form.end_date_field.render_kw = {"value": datetime.today().date()}  # + ""}

        print("HELlOOOOOOOOOOOOOOOO")

        if button_form.is_submitted():
            if button_form.start_field.data:
                print("STARTING COLLECTION")
                button_form.start_field.render_kw = {'disabled': 'disabled'}
                button_form.start_savepoint_field.render_kw = {'disabled': 'disabled'}
                if not config.collection_running:
                    x = threading.Thread(target=twitter_watcher.collection, daemon=True)
                    x.start()
                    config.collection_running = True
                    return render_template('edit_collection.html', button_form=EditCollectionParametersForm())
                else:
                    flash("Error: Collection is already running", "error")
            elif button_form.start_savepoint_field.data:
                if not (os.path.isfile("./savepoint/savepoint.json") and os.path.isfile("./savepoint/people.csv")):
                    flash("Error: Cant load from savepoint cause it doesn't exist", "error")
                print("STARTING COLLECTION")

                try:
                    set_bearer_token(button_form.token_switch_field.data, button_form.token_field.data)
                except Exception as e:
                    button_form.submit_field.render_kw = {}
                    button_form.start_field.render_kw = {'disabled': 'disabled'}
                    flash("Error: '" + str(e) + "'", "error")
                    return render_template(
                        'start_collection.html',
                        button_form=button_form
                    )

                button_form.start_field.render_kw = {'disabled': 'disabled'}
                button_form.start_savepoint_field.render_kw = {'disabled': 'disabled'}
                if not config.collection_running:
                    x = threading.Thread(target=twitter_watcher.collection, args=(True,), daemon=True)
                    x.start()
                    config.collection_running = True
                    return render_template('edit_collection.html', button_form=EditCollectionParametersForm())
                else:
                    flash("Error: Collection is already running", "error")
            elif button_form.validate():
                print("SETTING PARAMETERS")
                if button_form.submit_field.data:
                    button_form.submit_field.render_kw = {'disabled': 'disabled'}

                    try:
                        set_bearer_token(button_form.token_switch_field.data, button_form.token_field.data)
                    except Exception as e:
                        button_form.submit_field.render_kw = {}
                        button_form.start_field.render_kw = {'disabled': 'disabled'}
                        flash("Error: '" + str(e) + "'", "error")
                        return render_template(
                            'start_collection.html',
                            button_form=button_form
                        )

                    config.tweetWords = button_form.filter_words_field.data.split(",")
                    config.tweetEmojis = button_form.filter_emojis_field.data.split(",")
                    config.tweetHashtags = button_form.filter_hashtags_field.data.split(",")
                    config.tweetHandles = button_form.filter_mentions_field.data.split(",")

                    try:
                        config.people = read_file_data(button_form.people_field.data,
                                                       button_form.people_separator_field.data)

                        twitter_watcher.check_input(config.people)
                    except Exception as e:
                        button_form.submit_field.render_kw = {}
                        button_form.start_field.render_kw = {'disabled': 'disabled'}
                        flash("Error while reading file:    '" + str(e) + "'", "error")
                        return render_template(
                            'start_collection.html',
                            button_form=button_form
                        )

                    config.start_date = datetime.strptime(button_form.start_date_field.data.strftime('%m-%d-%y'),
                                                          '%m-%d-%y')
                    if not button_form.no_end_date_field.data:
                        config.end_date = datetime.strptime(button_form.end_date_field.data.strftime('%m-%d-%y'),
                                                            '%m-%d-%y')

                    print("END_DATE STEPS:", button_form.steps_field.data)
                    start_date = datetime.strptime(button_form.start_date_field.data.strftime('%m-%d-%y'), '%m-%d-%y')
                    end_date = datetime.strptime(button_form.end_date_field.data.strftime('%m-%d-%y'), '%m-%d-%y')
                    if (button_form.no_end_date_field.data or end_date.date() > datetime.today().date()) \
                            and config.Timesteps(button_form.steps_field.data) == config.Timesteps.NO_STEPS:
                        flash(
                            "Error: Time steps are needed when there is no end date or the end date lies in the future",
                            "error")
                        button_form.submit_field.render_kw = {}
                        button_form.start_field.render_kw = {"disabled": "disabled"}
                        return render_template(
                            'start_collection.html',
                            button_form=button_form
                        )
                    elif (not button_form.no_end_date_field.data) and start_date.date() >= end_date.date():
                        flash(
                            "Error: End date must be after start date",
                            "error")
                        button_form.submit_field.render_kw = {}
                        return render_template(
                            'start_collection.html',
                            button_form=button_form
                        )
                    else:
                        config.time_step_size = config.Timesteps(button_form.steps_field.data)
                        config.end_date = None

                    config.do_bot_detection = button_form.bot_detection_field.data
                    config.do_sentiment_analysis = button_form.sentiment_analysis_field.data

                    print("DATA:")
                    print(config.tweetWords)
                    print(config.tweetEmojis)
                    print(config.tweetHashtags)
                    print(config.tweetHandles)
                    print(config.people)
                    print(config.start_date)
                    print(config.end_date)

                    button_form.start_field.render_kw = {}
                    button_form.submit_field.render_kw = {"value": "Edit Parameters"}

        return render_template(
            'start_collection.html',
            button_form=button_form
        )

    else:  # TODO: Check if collection has stopped unintentionally and, if so, why and output to the user
        button_form = EditCollectionParametersForm()

        if button_form.is_submitted():
            if button_form.stop_field.data or button_form.pause_field.data:
                if button_form.pause_field.data:  # Switch buttons and stop/run collection
                    if not collection_paused:
                        collection_paused = True
                        button_form.pause_field.render_kw = {"class": "btn-success", "value": "Resume Collection",
                                                             "disabled": "disabled"}
                        twitter_watcher.stop_collection_process()
                        while config.collection_running:
                            time.sleep(0.2)
                        button_form.pause_field.render_kw = {"class": "btn-success", "value": "Resume Collection"}
                    else:
                        collection_paused = False
                        x = threading.Thread(target=twitter_watcher.collection, args=(True,), daemon=True)
                        x.start()
                        button_form.pause_field.render_kw = {"class": "btn-warning", "value": "Pause Collection"}

                elif button_form.stop_field.data:
                    button_form.pause_field.render_kw = {"disabled": "disabled"}
                    button_form.stop_field.render_kw = {"disabled": "disabled"}
                    twitter_watcher.stop_collection_process()
                    collection_paused = False
                    while config.collection_running:
                        time.sleep(0.2)

                    return redirect('/TwitterWatcher')

            elif button_form.validate():
                if button_form.submit_field.data:
                    button_form.submit_field.render_kw = {'disabled': 'disabled'}

                    config.added_filters = {"emojis": button_form.add_emojis_field.data.split(","),
                                            "keywords": button_form.add_words_field.data.split(","),
                                            "hashtags": button_form.add_hashtags_field.data.split(","),
                                            "handles": button_form.add_mentions_field.data.split(",")}
                    config.removed_filters = {"emojis": button_form.remove_emojis_field.data.split(","),
                                              "keywords": button_form.remove_words_field.data.split(","),
                                              "hashtags": button_form.remove_hashtags_field.data.split(","),
                                              "handles": button_form.remove_mentions_field.data.split(",")}

                    try:
                        if button_form.add_people_field.data is not None:
                            config.added_people = read_file_data(button_form.add_people_field.data,
                                                                 button_form.add_people_separator_field.data)
                            twitter_watcher.check_input(config.added_people)
                        if button_form.remove_people_field.data is not None:
                            config.removed_people = read_file_data(button_form.remove_people_field.data,
                                                                   button_form.remove_people_separator_field.data)
                            twitter_watcher.check_input(config.removed_people)
                    except Exception as e:
                        button_form.submit_field.render_kw = {}
                        flash("Error while reading file:    '" + str(e) + "'")
                        return render_template(
                            'edit_collection.html',
                            button_form=button_form
                        )

                    config.do_bot_detection = button_form.bot_detection_field.data
                    config.do_sentiment_analysis = button_form.sentiment_analysis_field.data

                    print("DATA:")
                    print(config.tweetWords)
                    print(config.tweetEmojis)
                    print(config.tweetHashtags)
                    print(config.tweetHandles)
                    print(config.people)

                    print(button_form.pause_field.name, button_form.pause_field.description)
                    # button_form.pause_field.render_kw = {"value": "Edit Changes"}
                    button_form.submit_field.render_kw = {"value": "Edit Changes"}

        return render_template(
            'edit_collection.html',
            button_form=button_form
        )


@app.route('/API', methods=['POST'])
@app.route('/TwitterWatcher/API', methods=['POST'])
def index_api():
    global collection_paused, x
    # config.collection_running = True  # TODO: Remove

    if not config.collection_running and not collection_paused:
        # Check if savepoint exists
        if request.form.get("user_savepoint", default=False, type=bool):
            if not (os.path.isfile("./savepoint/savepoint.json") and os.path.isfile("./savepoint/people.csv")):
                return "Error: Cant load from savepoint cause it doesn't exist", "error", 400

            x = threading.Thread(target=twitter_watcher.collection, args=(True,), daemon=True)
            x.start()
            config.collection_running = True
        else:
            print("HELlOOOOOOOOOOOOOOOO")

            try:
                bearer_token = request.form.get("bearer_token", default=None, type=str)
                if bearer_token is not None:
                    set_bearer_token(False, bearer_token)
                else:
                    set_bearer_token(True, "")
            except Exception as e:
                return "Error: '" + str(e) + "'", 400

            config.tweetWords = request.form.get("filter_words", default="", type=str).split(",")
            config.tweetEmojis = request.form.get("filter_emojis", default="", type=str).split(",")
            config.tweetHashtags = request.form.get("filter_hashtags", default="", type=str).split(",")
            config.tweetHandles = request.form.get("filter_mentions", default="", type=str).split(",")

            try:
                people_file = request.files["people_file"]  # TODO: Test whether this works
                people_separator = request.form.get("people_separator", default=",", type=str)
                config.people = read_file_data(people_file, people_separator)

                twitter_watcher.check_input(config.people)
            except Exception as e:
                return "Error while reading file:    '" + str(e) + "'", 400

            try:
                config.start_date = datetime.strptime(
                    request.form.get("start_date", default=datetime.today().date().strftime('%m-%d-%Y'), type=str),
                    '%m-%d-%Y')
                end_date_str = request.form.get("end_date", default=None, type=str)
                if end_date_str is not None:
                    config.end_date = datetime.strptime(end_date_str, '%m-%d-%Y')
                else:
                    config.end_date = None
            except Exception as e:
                return "Error while parsing collection dates", 400

            time_step_size_int = request.form.get("time_steps", default=1, type=int)
            if (config.end_date is None or config.end_date.date() > datetime.today().date()) \
                    and config.Timesteps(time_step_size_int) == config.Timesteps.NO_STEPS:
                return "Error: Time steps are needed when there is no end date or the end date lies in the future", 400
            elif config.end_date is not None and config.start_date.date() >= config.end_date.date():
                return "Error: End date must be after start date", 400
            else:
                config.time_step_size = config.Timesteps(time_step_size_int)

            config.do_bot_detection = request.form.get("do_bot_detection", default=False, type=bool)
            config.do_sentiment_analysis = request.form.get("do_sentiment_analysis_field", default=False, type=bool)

            print(request.form)
            print("DATA:")
            print(config.time_step_size)
            print(config.tweetWords)
            print(config.tweetEmojis)
            print(config.tweetHashtags)
            print(config.tweetHandles)
            print(config.people)
            print(config.start_date)
            print(config.end_date)

            x = threading.Thread(target=twitter_watcher.collection, daemon=True)
            x.start()
            config.collection_running = True

            return "Collection Successfully started! ", 200

    else:
        if request.form.get("switch_pause_collection", default=False, type=bool):
            if not collection_paused:
                collection_paused = True
                twitter_watcher.stop_collection_process()
                while config.collection_running:
                    time.sleep(0.2)
            else:
                collection_paused = False
                x = threading.Thread(target=twitter_watcher.collection, args=(True,), daemon=True)  # TODO: Test regarding the file stuff here
                x.start()

        elif request.form.get("stop_collection", default=False, type=bool):
            twitter_watcher.stop_collection_process()
            collection_paused = False
            while config.collection_running:
                time.sleep(0.2)

            return "Collection successfully stopped!", 200

        else:
            config.added_filters = {"emojis": request.form.get("add_emojis", default="", type=str).split(","),
                                    "keywords": request.form.get("add_words", default="", type=str).split(","),
                                    "hashtags": request.form.get("add_hashtags", default="", type=str).split(","),
                                    "handles": request.form.get("add_mentions", default="", type=str).split(",")}
            config.removed_filters = {"emojis": request.form.get("remove_emojis", default="", type=str).split(","),
                                      "keywords": request.form.get("remove_words", default="", type=str).split(","),
                                      "hashtags": request.form.get("remove_hashtags", default="", type=str).split(","),
                                      "handles": request.form.get("remove_mentions", default="", type=str).split(",")}

            try:
                add_people_file = request.files["add_people_file"]  # TODO: Test whether this works
                add_people_separator = request.form.get("add_people_separator", default=",", type=str)
                remove_people_file = request.files["remove_people_file"]  # TODO: Test whether this works
                remove_people_separator = request.form.get("remove_people_separator", default=",", type=str)

                if add_people_file is not None:
                    config.added_people = read_file_data(add_people_file, add_people_separator)
                    twitter_watcher.check_input(config.added_people)
                if remove_people_file is not None:
                    config.removed_people = read_file_data(remove_people_file, remove_people_separator)
                    twitter_watcher.check_input(config.removed_people)
            except Exception as e:
                return "Error while reading file:    '" + str(e) + "'", 400

            config.do_bot_detection = request.form.get("do_bot_detection", default=False, type=bool)
            config.do_sentiment_analysis = request.form.get("do_sentiment_analysis_field", default=False,
                                                            type=bool)

            print("DATA:")
            print(config.tweetWords)
            print(config.tweetEmojis)
            print(config.tweetHashtags)
            print(config.tweetHandles)
            print(config.people)

            return "Collection successfully resumed!", 200


@app.route('/progress')
def progress():
    people_fetched = 0.0
    if twitter_watcher.current_person is not None:
        people_fetched = 100 * ((twitter_watcher.current_person[0] + 1) / config.people.shape[0])

    steps_fetched = 100
    if config.end_date is not None:
        i, i_max, end_date_incr = 1, 1, twitter_watcher.incr_date_by_timestep(config.start_date, config.time_step_size)
        while end_date_incr.date() < config.end_date.date():
            end_date_incr = twitter_watcher.incr_date_by_timestep(end_date_incr, config.time_step_size)
            if end_date_incr.date() < twitter_watcher.current_end_date.date():
                i += 1
            i_max += 1

        steps_fetched = 100 * (i / i_max)

    return '{"people_percentage":' + str(int(people_fetched)) + ',"step_kind":"' + config.Timesteps(
        config.time_step_size).name \
           + '","steps_percentage":' + str(int(steps_fetched)) + '}'
