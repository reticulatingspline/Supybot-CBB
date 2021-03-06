###
# Copyright (c) 2013-2014, spline
# All rights reserved.
#
#
###
# my libs
from base64 import b64decode
import cPickle as pickle
from BeautifulSoup import BeautifulSoup
import sqlite3
import os.path
import datetime # utc time.
from itertools import chain
# extra supybot libs
import supybot.conf as conf
import supybot.schedule as schedule
import supybot.ircmsgs as ircmsgs
# std supybot libs
import supybot.utils as utils
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
try:
    from supybot.i18n import PluginInternationalization
    _ = PluginInternationalization('CBB')
except:
    # Placeholder that allows to run the plugin on a bot
    # without the i18n module
    _ = lambda x:x

class CBB(callbacks.Plugin):
    """Add the help for "@plugin help CBB" here
    This should describe *how* to use this plugin."""
    threaded = True

    def __init__(self, irc):
        self.__parent = super(CBB, self)
        self.__parent.__init__(irc)
        # our cbblive db.
        self._db = os.path.abspath(os.path.dirname(__file__)) + '/db/cbb.db'
        # initial states for channels.
        self.channels = {} # dict for channels with values as teams/ids
        self._loadpickle() # load saved data.
        # initial states for games.
        self.games = None
        self.nextcheck = None
        # fetchhost system.
        self.fetchhost = None
        self.fetchhostcheck = None
        # rankings.
        self.rankings = {}
        self.rankingstimer = None
        # fill in the blanks.
        if not self.games:
            self.games = self._fetchgames()
        # setup the function for cron.
        def checkcbbcron():
            try:
                self.checkcbb(irc)
            except Exception, e: # something broke. The plugin will stop itself from reporting.
                self.log.error("cron: ERROR :: {0}".format(e))
                self.nextcheck = self._utcnow()+72000 # add some major delay so the plugin does not spam.
        # and add the cronjob.
        try: # add our cronjob.
            schedule.addPeriodicEvent(checkcbbcron, 30, now=True, name='checkcbb')
        except AssertionError:
            try:
                schedule.removeEvent('checkcbb')
            except KeyError:
                pass
            schedule.addPeriodicEvent(checkcbbcron, 30, now=True, name='checkcbb')

    def die(self):
        try: # remove cronjob.
            schedule.removeEvent('checkcbb')
        except KeyError:
            pass
        self.__parent.die()

    ######################
    # INTERNAL FUNCTIONS #
    ######################

    def _httpget(self, url):
        """General HTTP resource fetcher."""

        # self.log.info(url)
        try:
            h = {"User-Agent":"Mozilla/5.0 (X11; Ubuntu; Linux i686; rv:17.0) Gecko/20100101 Firefox/17.0"}
            page = utils.web.getUrl(url, headers=h)
            return page
        except utils.web.Error as e:
            self.log.error("ERROR opening {0} message: {1}".format(url, e))
            return None

    def _utcnow(self):
        """Calculate Unix timestamp from GMT."""

        ttuple = datetime.datetime.utcnow().utctimetuple()
        _EPOCH_ORD = datetime.date(1970, 1, 1).toordinal()
        year, month, day, hour, minute, second = ttuple[:6]
        days = datetime.date(year, month, 1).toordinal() - _EPOCH_ORD + day - 1
        hours = days*24 + hour
        minutes = hours*60 + minute
        seconds = minutes*60 + second
        return seconds

    ###########################################
    # INTERNAL CHANNEL POSTING AND DELEGATION #
    ###########################################

    def _post(self, irc, awayid, homeid, message):
        """Posts message to a specific channel."""

        # how this works is we have an incoming away and homeid. we find out their conference ids.
        # against the self.channels dict (k=channel, v=set of #). then, if any of the #'s match in the v
        # we insert this back into postchans so that the function posts the message into the proper channel(s).
        if len(self.channels) == 0: # first, we have to check if anything is in there.
            #self.log.error("ERROR: I do not have any channels to output in.")
            return
        # we do have channels. lets go and check where to put what.
        confids = self._tidstoconfids(awayid, homeid) # grab the list of conf ids.
        if not confids: # failsafe here.
            self.log.error("_post: something went wrong with confids for awayid: {0} homeid: {1} m: {2} confids: {3}".format(awayid, homeid, message, confids))
            return
        postchans = [k for (k, v) in self.channels.items() if __builtins__['any'](z in v for z in confids)]
        # iterate over each.
        for postchan in postchans:
            try:
                irc.queueMsg(ircmsgs.privmsg(postchan, message))
            except Exception as e:
                self.log.error("ERROR: Could not send {0} to {1}. {2}".format(message, postchan, e))

    ##############################
    # INTERNAL CHANNEL FUNCTIONS #
    ##############################

    def _loadpickle(self):
        """Load channel data from pickle."""

        try:
            datafile = open(conf.supybot.directories.data.dirize(self.name()+".pickle"), 'rb')
            try:
                dataset = pickle.load(datafile)
            finally:
                datafile.close()
        except IOError:
            return False
        # restore.
        self.channels = dataset["channels"]
        return True

    def _savepickle(self):
        """Save channel data to pickle."""

        data = {"channels": self.channels}
        try:
            datafile = open(conf.supybot.directories.data.dirize(self.name()+".pickle"), 'wb')
            try:
                pickle.dump(data, datafile)
            finally:
                datafile.close()
        except IOError:
            return False
        return True

    ##################################
    # TEAM DB AND DATABASE FUNCTIONS #
    ##################################

    def _tidwrapper(self, tid, d=False):
        """TeamID wrapper."""

        # first, try to see if it's in the database.
        dblookup = self._tidtoname(tid, d=d)
        if dblookup: # return the DB entry.
            return dblookup
        else:
            return None

    def _tidtoname(self, tid, d=False):
        """Return team name for teamid from database. Use d=True to return as dict."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT team, tid FROM teams WHERE id=?", (tid,))
            row = cursor.fetchone()
        # now return the name.
        if not row: # didn't find. we just return None here.
            return None
        else: # did find.
            if row[1] != '': # some are empty. we did get something back.
                # check if we have rankings and team is in rankings dict.
                if ((self.rankings) and (row[1] in self.rankings)): # in there so append the #.
                    if d: # return as dict.
                        return {'rank':self.rankings[row[1]], 'team':row[0].encode('utf-8')}
                    else: # normal return
                        return "({0}){1}".format(self.rankings[row[1]], row[0].encode('utf-8'))
                else: # no rankings or not in the table so just return the teamname.
                    if d: # return as dict.
                        return {'team': row[0].encode('utf-8')}
                    else: # normal return
                        return row[0].encode('utf-8')
            else: # return just the team.
                if d: # return as dict.
                    return {'team': row[0].encode('utf-8')}
                else: # normal return
                    return row[0].encode('utf-8')

    def _tidstoconfids(self, tid1, tid2):
        """Fetch the conference ID for a team."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT DISTINCT conf FROM teams WHERE id IN (?, ?)"
            cursor.execute(query, (tid1, tid2,))
            item = [i[0] for i in cursor.fetchall()] # put the ids into a list.
            # check to make sure we have something.
            if len(item) == 0:
                return None
            else:
                return item

    def _confs(self):
        """Return a dict containing all conferences and their ids: k=id, v=confs."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT id, conference FROM confs"
            cursor.execute(query)
            c = dict((i[0], i[1]) for i in cursor.fetchall())
            return c

    def _validconf(self, confname):
        """Validate a conf and return its ID."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT id FROM confs WHERE conference=?"
            cursor.execute(query, (confname,))
            row = cursor.fetchone()
        # now return the name.
        if row:
            return row[0]
        else:
            return None

    def _tidtoconf(self, tid):
        """Fetch what conference name (string) a team is in."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT conference FROM confs WHERE id IN (SELECT conf FROM teams WHERE id=?)"
            cursor.execute(query, (tid,))
            conference = cursor.fetchone()[0]
        # now return.
        return conference.encode('utf-8')

    def _confidtoname(self, confid):
        """Validate a conf and return its ID."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT conference FROM confs WHERE id=?"
            cursor.execute(query, (confid,))
            row = cursor.fetchone()
        # now return the name.
        if row:
            return row[0].encode('utf-8')
        else:
            return None

    def _d1confs(self):
        """Return a list of all D1 conference ids."""

        with sqlite3.connect(self._db) as conn:
            cursor = conn.cursor()
            query = "SELECT id FROM confs WHERE division=1"
            cursor.execute(query)
            confids = [i[0] for i in cursor.fetchall()]
        # now return.
        return confids

    ####################
    # FETCH OPERATIONS #
    ####################

    def _fetchhost(self):
        """Return the host for fetch operations."""

        utcnow = self._utcnow()
        # if we don't have the host, lastchecktime, or fetchhostcheck has passed, we regrab.
        if ((not self.fetchhostcheck) or (not self.fetchhost) or (self.fetchhostcheck < utcnow)):
            url = b64decode('aHR0cDovL2F1ZC5zcG9ydHMueWFob28uY29tL2Jpbi9ob3N0bmFtZQ==')
            html = self._httpget(url) # try and grab.
            if not html:
                self.log.error("ERROR: _fetchhost: could not fetch {0}")
                return None
            # now that we have html, make sure its valid.
            if html.startswith("aud"):
                fhurl = 'http://%s' % (html.strip())
                self.fetchhost = fhurl # set the url.
                self.fetchhostcheck = utcnow+3600 # 1hr from now.
                return fhurl
            else:
                self.log.error("ERROR: _fetchhost: returned string didn't match aud. We got {0}".format(html))
                return None
        else: # we have a host and it's under the cache time.
            return self.fetchhost

    def _fetchgames(self, filt=True):
        """Return the games.txt data into a processed dict. Set filter=False for all games."""

        url = self._fetchhost() # grab the host to check.
        if not url: # didn't get it back.
            self.log.error("ERROR: _fetchgames broke on _fetchhost()")
            return None
        else: # we got fetchhost. create the url.
            url = "%s/ncaab/games.txt" % (url)
        # now we try and fetch the actual url with data.
        html = self._httpget(url)
        if not html:
            self.log.error("ERROR: _fetchgames: could not fetch {0} :: {1}".format(url))
            return None
        # now turn the "html" into a list of dicts.
        newgames = self._txttodict(html, filt=filt)
        if not newgames: # no new games for some reason.
            return None
        else: # we have games. return.
            return newgames

    def _txttodict(self, txt, filt):
        """Games game lines from fetchgames and turns them into a list of dicts. filt=True to limit games."""

        lines = txt.splitlines() # split.
        games = {} # container.

        for line in lines: # iterate over.
            if line.startswith('g|'): # only games.
                cclsplit = line.split('|') # split.
                # g|201311100059|416|59|S|0|1|20:00|0|0|1384104600|1|4|1|4
                # 0 = g, 1 = gid, 2 = at, 3 = ht, 4 = status, 5 = ?, 6 = half, 7 = time, 8 = as, 9 = hs, 10 = ?, 11 = ?, 12 = ?, 13 = ?
                t = {} # tmp dict for each line.
                t['awayteam'] = cclsplit[2]
                t['hometeam'] = cclsplit[3]
                t['status'] = cclsplit[4]
                t['period'] = cclsplit[6]
                t['time'] = cclsplit[7]
                t['awayscore'] = int(cclsplit[8])
                t['homescore'] = int(cclsplit[9])
                t['start'] = int(cclsplit[10])
                # now we need to test if we should filter.
                if filt: # True. filtertest will be True if we should include the game. False if we should skip/pass over.
                    filtertest = self._filtergame(t['awayteam'], t['hometeam'])
                    if filtertest: # True. add into games dict.
                       games[cclsplit[1]] = t
                else: # False. Don't filter. Add everything.
                    games[cclsplit[1]] = t
        # process if we have games or not.
        if len(games) == 0: # no games.
            self.log.error("ERROR: No matching lines in _txttodict")
            self.log.error("ERROR: _txttodict: {0}".format(txt))
            # should we add delay here?
            return None
        else:
            return games

    def _filtergame(self, at, ht):
        """With at and ht ids, we need to test if we should filter."""

        # check to see what activeconfs comes from.
        if len(self.channels) != 0: # we have "active" confs. consolidate the sets from each active channel.
            activeconfs = set(chain.from_iterable(([v for (k, v) in self.channels.items()])))
        else: # no active confs so we just grab D1 conf ids.
            activeconfs = set(self._d1confs())
        # now lets take the at+ht ids and test.
        teamidslist = self._tidstoconfids(at, ht) # grab the list of conf ids for this game.
        if teamidslist: # failsafe but should never trigger.
            if not activeconfs.isdisjoint(teamidslist): # this will be True if one of the ids from teamidslist = in activeconfs.
                return True
            else: # at/ht (game) is NOT in activeconfs.
                return False
        else: # missing teams.. sigh.
            self.log.info("_filtergame: teamidslist failed on one of AT: {0} HT: {1}".format(at, ht))
            return False

    def _rankings(self):
        """Fetch the AP/BCS rankings for display."""

        # first, we need the time.
        utcnow = self._utcnow()
        # now determine if we should repopulate.
        if ((len(self.rankings) == 0) or (not self.rankingstimer) or (utcnow > self.rankingstimer)):
            # fetch AP rankings.
            url = b64decode('aHR0cDovL3Nwb3J0cy55YWhvby5jb20vbmNhYS9iYXNrZXRiYWxsL3BvbGxzP3BvbGw9MQ==')
            # fetch url
            html = self._httpget(url)
            if not html:
                self.log.error("ERROR: Could not fetch {0}".format(url))
                self.rankingstimer = utcnow+60
                self.log.info("_rankings: AP html failed")
            try: # parse the table and populate.
                soup = BeautifulSoup(html)
                table = soup.find('table', attrs={'id':'ysprankings-results-table'})
                rows = table.findAll('tr')[1:26] # just to make sure.
                for i, row in enumerate(rows):
                    team = row.find('a')['href'].split('/')[5] # find the team abbr.
                    self.rankings[team] = i+1 # populate dict.
                # now finalize.
                self.rankingstimer = utcnow+86400 # 24hr.
                self.log.info("_rankings: updated AP rankings.")
            except Exception, e: # something went wrong.
                self.log.error("_rankings: AP ERROR: {0}".format(e))
                self.rankingstimer = utcnow+60 # rerun in one minute.


    def _gctosec(self, s):
        """Convert seconds of clock into an integer of seconds remaining."""

        if s.startswith(":"):  # strip leading ':'
            s = s[1:]
        # now, if we're over 60s, time will look like 1:01. if we're under, it is :50.0
        if '.' in s:  # under 60s.
            s = s.replace(':', '')  # strip :, so we're left with 50.0.
            return int(float(s))  # convert to integer from float.
        else:  # we're over 60s. Time will look like 1:01.
            l = s.split(':')  # split and do some math below
            return (int(l[0]) * 60 + int(l[1]))

    ######################
    # CHANNEL MANAGEMENT #
    ######################

    def cbbliveon(self, irc, msg, args):
        """
        Re-enable CBB updates in channel.
        Must be enabled by an op in the channel scores are already enabled for.
        """

        # channel
        channel = channel.lower()
        # check if op.
        if not irc.state.channels[channel].isOp(msg.nick):
            irc.reply("ERROR: You must be an op in this channel for this command to work.")
            return
        # check if channel is already on.
        if channel in self.channels:
            irc.reply("ERROR: {0} is already enabled for {1} updates.".format(channel, self.name()))
        # we're here if it's not. let's re-add whatever we have saved.
        # most of this is from _loadchannels
        try:
            datafile = open(conf.supybot.directories.data.dirize(self.name()+".pickle"), 'rb')
            try:
                dataset = pickle.load(datafile)
            finally:
                datafile.close()
        except IOError:
            irc.reply("ERROR: I could not open the {0} pickle to restore. Something went horribly wrong.".format(self.name()))
            return
        # now check if channels is in the dataset from the pickle.
        if channel in dataset['channels']: # it is. we're good.
            self.channels[channel] = dataset['channels'][channel] # restore it.
        else:
            irc.reply("ERROR: {0} is not in the saved channel list. Please use cbbchannel to add it.".format(channel))

    cbbliveon = wrap(cbbliveon, [('channel')])

    def cbbliveoff(self, irc, msg, args):
        """
        Disable CBB scoring updates in a channel.
        Must be issued by an op in a channel it is enabled for.
        """

        # channel
        channel = channel.lower()
        # check if op.
        if not irc.state.channels[channel].isOp(msg.nick):
            irc.reply("ERROR: You must be an op in this channel for this command to work.")
            return
        # check if channel is already on.
        if channel not in self.channels:
            irc.reply("ERROR: {0} is not in self.channels. I can't disable updates for a channel I don't have configured.".format(channel))
            return
        else: # channel is in the dict so lets do a temp disable by deleting it.
            del self.channels[channel]
            irc.reply("I have successfully disabled {0} updates in {0}".format(self.name(), channel))

    cbbliveoff = wrap(cbbliveoff, [('channel')])

    def cbbchannel(self, irc, msg, args, op, optchannel, optarg):
        """<add|list|del|confs> <#channel> <CONFERENCE|D1>

        Add or delete conference(s)/D1 from a specific channel's output.
        Use conference name or D1 for all D1 confs on add/del ops. Otherwise, can only specify one at a time.

        Ex: add #channel1 D1 OR add #channel2 SEC OR del #channel1 ALL OR list OR confs
        """

        # first, lower operation.
        op = op.lower()
        # next, make sure op is valid.
        validop = ['add', 'list', 'del', 'confs']
        if op not in validop: # test for a valid operation.
            irc.reply("ERROR: '{0}' is an invalid operation. It must be be one of: {1}".format(op, " | ".join([i for i in validop])))
            return
        # if we're not doing list (add or del) make sure we have the arguments.
        if ((op != 'list') and (op != 'confs')):
            if not optchannel or not optarg: # add|del need these.
                irc.reply("ERROR: add and del operations require a channel and team. Ex: add #channel SEC OR del #channel SEC")
                return
            # we are doing an add/del op.
            optchannel = optchannel.lower()
            # make sure channel is something we're in
            if op == 'add': # check for channel on add only.
                if optchannel not in irc.state.channels:
                    irc.reply("ERROR: '{0}' is not a valid channel. You must add a channel that we are in.".format(optchannel))
                    return
            # test for valid conf now. we have a "special" D1 here to add all D1.
            if ((optarg == "D1") or (optarg == "d1")):  # we want to add all D1 confs.
                confid = self._d1confs()  # grab the list of D1 confs.
            else:  # not D1, so check the argument.
                confid = self._validconf(optarg)
            # validate it before we proceed.
            if not confid: # invalid arg(conf)
                irc.reply("ERROR: '{0}' is an invalid conference. Must be one of: {1}".format(optarg, " | ".join(sorted(self._confs().values()))))
                return
        # main meat part.
        # now we handle each op individually.
        if op == 'add': # add output to channel. we use a set so there is no need to check for dupes.
            if isinstance(confid, list):  # confids = list, ie: D1 is optarg
                for cid in confid:  # iterate over each and add.
                    self.channels.setdefault(optchannel, set()).add(cid) # add it.
            else:
                self.channels.setdefault(optchannel, set()).add(confid) # add it.
            # now output and save.
            irc.reply("I have added {0} into {1}".format(optarg, optchannel))
            self._savepickle() # save.
        elif op == 'confs': # list confs.
            irc.reply("Valid Confs for cbbchannel: {0}".format(" | ".join(sorted(self._confs().values()))))
        elif op == 'list': # list channels.
            if len(self.channels) == 0: # no channels.
                irc.reply("ERROR: I have no active channels defined. Please use the cbbchannel add operation to add a channel.")
            else: # we do have channels.
                for (k, v) in self.channels.items(): # iterate through and output
                    irc.reply("{0} :: {1}".format(k, " | ".join([self._confidtoname(q) for q in v])))
        elif op == 'del': # delete an item from channels.
            if optchannel in self.channels:
                if isinstance(confid, list):  # confids = list, ie: D1 is optarg.
                    for cid in confid:  # iterate over each.
                        self.channels[optchannel].discard(cid) # remove it. don't raise KeyError if not.
                    # report success (no way to know).
                    irc.reply("I have successfully removed all D1 confs from {0}".format(optchannel))
                else:  # confid is NOT a list, ie: individual string.
                    if confid in self.channels[optchannel]: # id is already in.
                        self.channels[optchannel].remove(confid) # remove it.
                        irc.reply("I have successfully removed {0} from {1}".format(optarg, optchannel))
                    else:
                        irc.reply("ERROR: I do not have {0} in {1}".format(optarg, optchannel))
                        return
                # now that we're done doing the delete operations, check if we have any left.
                if len(self.channels[optchannel]) == 0: # none left.
                    del self.channels[optchannel] # delete the channel key.
                # save the pickle.
                self._savepickle() # save it.
            else:
                irc.reply("ERROR: I do not have {0} in {1}".format(optarg, optchannel))

    cbbchannel = wrap(cbbchannel, [('checkCapability', 'admin'), ('somethingWithoutSpaces'), optional('channel'), optional('text')])

    #######################
    # INTERNAL FORMATTING #
    #######################

    def _boldleader(self, awayteam, awayscore, hometeam, homescore):
        """Conveinence function to bold the leader."""

        if (int(awayscore) > int(homescore)): # visitor winning.
            return "{0} {1} {2} {3}".format(ircutils.bold(awayteam), ircutils.bold(awayscore), hometeam, homescore)
        elif (int(awayscore) < int(homescore)): # home winning.
            return "{0} {1} {2} {3}".format(awayteam, awayscore, ircutils.bold(hometeam), ircutils.bold(homescore))
        else: # tie.
            return "{0} {1} {2} {3}".format(awayteam, awayscore, hometeam, homescore)

    def _scoreformat(self, v):
        """Conveinence function to format score reporting for alerts/halftime/etc."""

        at = self._tidwrapper(v['awayteam']) # fetch visitor.
        ht = self._tidwrapper(v['hometeam']) # fetch home.
        gamestr = self._boldleader(at, v['awayscore'], ht, v['homescore'])
        # format time
        if v['time'].startswith(':'):  # below 1 minute so time looks like :57.6
            t =  v['time'][1:]  # strip the :
        else:  # regular time
            t = v['time']
        # format period and construct string.
        if (int(v['period']) > 2):  # if > 2, we're in "overtime".
            qtrstr = "{0} {1}OT".format(t, int(v['period'])-2)
        else:  # in 1st or 2nd half.
            qtrstr = "{0} {1}".format(t, utils.str.ordinal(v['period']))
        # finally, construct the rest
        mstr = "{0} :: {1}".format(gamestr, ircutils.bold(qtrstr))
        # now return
        return mstr

    ###################
    # PUBLIC COMMANDS #
    ###################

    def cbbgames(self, irc, msg, args):
        """
        Display all current games in the self.games. (DEBUG COMMAND)
        """

        #games = self._fetchgames(filt=True)
        games = self.games
        if not games:
            irc.reply("ERROR: Fetching games.")
            return
        for (k, v) in games.items():
            at = self._tidwrapper(v['awayteam'])
            ht = self._tidwrapper(v['hometeam'])
            irc.reply("{0} v. {1} :: {2}".format(at, ht, v))

    cbbgames = wrap(cbbgames)

    def checkcbb(self, irc):
    #def checkcbb(self, irc, msg, args):
        """
        Main loop.
        """

        # debug.
        self.log.info("checkcbb: starting...")
        # before anything, check if nextcheck is set and is in the future.
        if self.nextcheck: # set
            utcnow = self._utcnow()
            if self.nextcheck > utcnow: # in the future so we backoff.
                self.log.info("checkcbb: nextcheck is {0}s from now".format(abs(utcnow-self.nextcheck)))
                return
            else: # in the past so lets reset it. this means that we've reached the time where firstgametime should begin.
                self.log.info("checkcbb: nextcheck has passed. we are resetting and continuing normal operations.")
                self.nextcheck = None
        # we must have initial games. bail if not.
        if not self.games:
            self.games = self._fetchgames()
            return
        # check and see if we have initial games, again, but bail if no.
        if not self.games:
            self.log.error("checkcbb: I did not have any games in self.games")
            return
        else: # setup the initial games.
            games1 = self.games
        # now we must grab the new status to compare to.
        games2 = self._fetchgames()
        if not games2: # something went wrong so we bail.
            self.log.error("checkcbb: fetching games2 failed.")
            return

        # before we run the main event handler, make sure we have rankings.
        self._rankings()
        # main handler for event changes.
        # we go through and have to match specific conditions based on changes.
        for (k, v) in games1.items(): # iterate over games.
            if k in games2: # must mate keys between games1 and games2.
                # ACTIVE GAME EVENTS HERE
                if ((v['status'] == "P") and (games2[k]['status'] == "P")):
                    # WE CHECK FOR THE 10 MINUTE MARK IN THE 1ST/2ND HALF AND FIRE THE SCORE HERE.
                    if ((v['time'] != games2[k]['time']) and (games2[k]['period'] in ("1", "2")) and (self._gctosec(v['time']) >= 600) and (self._gctosec(games2[k]['time']) < 600)):
                        self.log.info("Should fire 10 minute score alert in {0}".format(k))
                        mstr = self._scoreformat(games2[k])  # send to score formatter.
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # WE NOW CHECK AT THE 1 MINUTE MARK OF THE 2ND HALF OR ABOVE (OT PERIODS) FOR A CLOSE SCORE (WITHIN 6 PTS) FOR NOTIFICATION.
                    if ((v['time'] != games2[k]['time']) and (int(games2[k]['period']) > 1) and (self._gctosec(v['time']) >= 60) and (self._gctosec(games2[k]['time']) < 60) and (abs(int(games2[k]['awayscore'])-int(games2[k]['homescore'])) < 8)):
                        self.log.info("Should fire 1 minute close score alert in {0}".format(k))
                        mstr = self._scoreformat(games2[k])  # send to score formatter.
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # HALFTIME IN
                    if ((v['time'] != games2[k]['time']) and (games2[k]['period'] == "1") and (games2[k]['time'] == ":00.0")):
                        self.log.info("Should fire halftime in {0}".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        gamestr = self._boldleader(at, games2[k]['awayscore'], ht, games2[k]['homescore'])
                        mstr = "{0} :: {1}".format(gamestr, ircutils.mircColor("HALFTIME", 'yellow'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # HALFTIME OUT
                    if ((v['period'] != games2[k]['period']) and (v['time'] != games2[k]['time']) and (games2[k]['period'] == "2") and (games2[k]['time'] == "20:00")):
                        self.log.info("Should fire 2nd half in {0}".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        gamestr = self._boldleader(at, games2[k]['awayscore'], ht, games2[k]['homescore'])
                        mstr = "{0} :: {1}".format(gamestr, ircutils.mircColor("Start 2nd Half", 'green'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # OT NOTIFICATION
                    if ((v['period'] != games2[k]['period']) and (int(games2[k]['period']) > 2)):
                        self.log.info("Should fire OT notification in {0}".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        gamestr = self._boldleader(at, games2[k]['awayscore'], ht, games2[k]['homescore'])
                        otper = "Start OT{0}".format(int(games2[k]['period'])-2) # should start with 3, which is OT1.
                        mstr = "{0} :: {1}".format(gamestr, ircutils.mircColor(otper, 'green'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # UPSET ALERT. CHECKS ONLY IN 2ND HALF AND ANY OT PERIOD.
                    if ((games2[k]['period'] >= "2") and (v['time'] != games2[k]['time']) and (self._gctosec(v['time']) >= 120) and (self._gctosec(games2[k]['time']) < 120)):
                        #self.log.info("inside upset alert {0}".format(k))
                        # fetch teams with ranking in dict so we can determine if there is a potential upset on hand.
                        at = self._tidwrapper(v['awayteam'], d=True) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam'], d=True) # fetch home.
                        # now we need to check if there is a ranking in either or both teams and
                        # act properly depending on the rank + score.
                        if (('rank' in at) or ('rank' in ht)): # require ranking. 3 scenarios: at ranked, ht ranked, both ranked.
                            #self.log.info("2nd upset alert in {0}".format(k))
                            awayscore = games2[k]['awayscore'] # grab the score.
                            homescore = games2[k]['homescore']
                            scorediff = abs(awayscore-homescore) # abs on the points diff.
                            upsetalert, potentialupsetalert, upsetstr = False, False, None # defaults.
                            if (('rank' in at) and ('rank' not in ht)): # away team ranked, home team is not.
                                #self.log.info("rank in at not ht {0}".format(k))
                                if homescore > awayscore: # ranked awayteam is losing.
                                    upsetalert = True
                                else:
                                    if scorediff < 7: # score is within a single possession.
                                        potentialupsetalert = True
                            elif (('rank' not in at) and ('rank' in ht)): # home team ranked, away is not.
                                #self.log.info("rank in ht not at {0}".format(k))
                                if awayscore > homescore: # ranked hometeam is losing.
                                    upsetalert = True
                                else:
                                    if scorediff < 7: # score is within a single possession.
                                        potentialupsetalert = True
                            else: # both teams are ranked, so we have to check what team is ranked higher and act accordingly.
                                #self.log.info("both teams ranked {0}".format(k))
                                if at['rank'] < ht['rank']: # away team ranked higher. (lower is higher)
                                    if homescore > awayscore: # home team is winning.
                                        upsetalert = True
                                    else:
                                        if scorediff < 7: # score is within a single possession.
                                            potentialupsetalert = True
                                else: # home team is ranked higher. (lower is higher)
                                    if awayscore > homescore: # away team is winning.
                                        upsetalert = True
                                    else:
                                        if scorediff < 7: # score is within a single possession.
                                            potentialupsetalert = True
                            # now that we're done, we check on upsetalert and potentialupsetalert to set upsetstr.
                            if upsetalert: # we have an upset alert.
                                upsetstr = ircutils.bold("AT&T UPSET ALERT")
                            elif potentialupsetalert: # we have a potential upset.
                                upsetstr = ircutils.bold("POTENTIAL AT&T UPSET ALERT")
                            # should we fire?
                            if upsetstr: # this was set above if conditions were met. so lets get our std gamestr, w/score, add the string, and post.
                                self.log.info("SHOULD BE POSTING ACTUAL UPSET ALERT STRING FROM {0}".format(k))
                                gamestr = self._boldleader(self._tidwrapper(v['awayteam']), games2[k]['awayscore'], self._tidwrapper(v['hometeam']), games2[k]['homescore'])
                                mstr = "{0} :: {1}".format(gamestr, upsetstr)
                                self._post(irc, v['awayteam'], v['hometeam'], mstr)
                # EVENTS OUTSIDE OF AN ACTIVE GAME.
                else:
                    # TIPOFF.
                    if ((v['status'] == "S") and (games2[k]['status'] == "P")):
                        self.log.info("{0} is tipping off.".format(k))
                        # now construct kickoff event.
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        atconf = self._tidtoconf(v['awayteam']) # fetch visitor conf.
                        htconf = self._tidtoconf(v['hometeam']) # fetch hometeam conf.
                        mstr = "{0}({1}) @ {2}({3}) :: {4}".format(ircutils.bold(at), atconf, ircutils.bold(ht), htconf, ircutils.mircColor("TIPOFF", 'green'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # GAME GOES FINAL.
                    if ((v['status'] == "P") and (games2[k]['status'] == "F")):
                        self.log.info("{0} is going final.".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        gamestr = self._boldleader(at, games2[k]['awayscore'], ht, games2[k]['homescore'])
                        if (int(games2[k]['period']) > 2):
                            fot = "F/OT{0}".format(int(games2[k]['period'])-2)
                            mstr = "{0} :: {1}".format(gamestr, ircutils.mircColor(fot, 'red'))
                        else:
                            mstr = "{0} :: {1}".format(gamestr, ircutils.mircColor("F", 'red'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # GAME GOES INTO A DELAY.
                    if ((v['status'] == "P") and (games2[k]['status'] == "D")):
                        self.log.info("{0} is going into delay.".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        mstr = "{0}@{1} :: {2}".format(at, ht, ircutils.mircColor("DELAY", 'yellow'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)
                    # GAME COMES OUT OF A DELAY.
                    if ((v['status'] == "D") and (games2[k]['status'] == "P")):
                        self.log.info("{0} is resuming from delay.".format(k))
                        at = self._tidwrapper(v['awayteam']) # fetch visitor.
                        ht = self._tidwrapper(v['hometeam']) # fetch home.
                        mstr = "{0}@{1} :: {2}".format(at, ht, ircutils.mircColor("RESUMED", 'green'))
                        self._post(irc, v['awayteam'], v['hometeam'], mstr)

        # done checking. copy new to self.games
        self.games = games2 # change status.
        # last, before we reset to check again, we need to verify some states of games in order to set sentinel or not.
        # STATUSES :: D = Delay, P = Playing, S = Future Game, F = Final, O = PPD
        # first, we grab all the statuses in newgames (games2)
        gamestatuses = set([i['status'] for i in games2.values()])
        self.log.info("GAMESTATUSES: {0}".format(gamestatuses))
        # next, check what the statuses of those games are and act accordingly.
        if (('D' in gamestatuses) or ('P' in gamestatuses)): # if any games are being played or in a delay, act normal.
            self.nextcheck = None # set to None to make sure we're checking on normal time.
        elif 'S' in gamestatuses: # no games being played or in delay, but we have games in the future. (ie: day games done but night games later)
            firstgametime = sorted([f['start'] for (i, f) in games2.items() if f['status'] == "S"])[0] # get all start times with S, first (earliest).
            utcnow = self._utcnow() # grab UTC now.
            if firstgametime > utcnow: # make sure it is in the future so lock is not stale.
                self.nextcheck = firstgametime # set to the "first" game with 'S'.
                self.log.info("checkcbb: we have games in the future (S) so we're setting the next check {0} seconds from now".format(firstgametime-utcnow))
            else: # firstgametime is NOT in the future. this is a problem.
                fgtdiff = abs(firstgametime-utcnow) # get how long ago the first game should have been.
                if fgtdiff < 3601: # if less than an hour ago, just basically pass.
                    self.nextcheck = None
                    self.log.info("checkcbb: firstgametime has passed but is under an hour so we resume normal operations.")
                else: # over an hour so we set firstgametime an hour from now.
                    self.nextcheck = utcnow+600
                    self.log.info("checkcbb: firstgametime is over an hour late so we're going to backoff for 10 minutes")
        else: # everything is "F" (Final). we want to backoff so we're not flooding.
            self.nextcheck = self._utcnow()+600 # 10 minutes from now.
            self.log.info("checkcbb: no active games and I have not got new games yet, so I am holding off for 10 minutes.")

    #checkcbb = wrap(checkcbb)

Class = CBB


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
