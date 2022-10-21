#!/usr/bin/env python

import asyncio
from datetime import datetime, timedelta
from json import JSONDecodeError
import json
from pyexpat.errors import messages
from pytz import timezone
import argparse
import re
import time
import requests
import cloudscraper
from bs4 import BeautifulSoup
from pynput import keyboard
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, NoSuchElementException, TimeoutException

import discord
from discord.ext import commands, tasks

COMBINATION = {keyboard.Key.ctrl, keyboard.Key.esc}


class Flag:
    OK = 0
    STOP = 1
    QUIT = 2


class KadWatcher(commands.Bot):
    def __init__(self, usr, pwd):
        intents = discord.Intents.default()
        super().__init__(intents=intents, command_prefix='?')
        self.usr = usr
        self.pwd = pwd
        self.kad_url = "https://www.neopets.com/games/kadoatery/index.phtml"
        self.scraper = cloudscraper.create_scraper(browser='chrome')

        self.login_attempts = 0
        self.current_kad = 0
        self.count = 0
        self.start_time = time.time()
        self.bot_status = Flag.OK
        self.current = set()
        keyboard.Listener(on_press=self.on_press, on_release=self.on_release).start()
        self.add_command(self.set_status)

    @commands.command()
    async def set_status(self, ctx, status='stop'):
        print('ayo')
        if status == 'quit':
            self.bot_status = Flag.QUIT
        elif status == 'stop':
            self.bot_status = Flag.STOP
        elif status == 'ok':
            self.bot_status = Flag.OK
        else:
            await ctx.send("Invalid command")
            return
        await ctx.send(f"Changed bot status to {status}")

    # Check for key combination to stop bot execution
    def on_press(self, key):
        if key in COMBINATION:
            self.current.add(key)
        if all(k in self.current for k in COMBINATION):
            print("Pulling the brakes!")
            self.bot_status = Flag.QUIT

    def on_release(self, key):
        try:
            self.current.remove(key)
        except KeyError:
            pass

    def create_browser(self):
        try:
            options = webdriver.ChromeOptions()
            options.add_argument('--headless')
            options.add_argument('--window-size=1366,768')
            return webdriver.Chrome(options=options)
        except WebDriverException:
            options = webdriver.FirefoxOptions()
            options.set_preference('permissions.default.stylesheet', 2)
            options.set_preference('permissions.default.image', 2)
            options.add_argument('--headless')
            options.add_argument('--window-size=1366,768')
            return webdriver.Firefox(options=options)

    def set_channel(self, channel):
        self.channel = channel

    async def setup_hook(self):
        self.check_for_refresh_bot.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')

    def login_selenium(self, usr, pwd):
        print(f"Logging in neopets")
        browser = self.create_browser()
        browser.delete_all_cookies()
        browser.get("https://www.neopets.com/login/")
        time.sleep(10)
        try:
            browser.find_element(by=By.ID, value="loginUsername").send_keys(usr)
            browser.find_element(by=By.ID, value="loginPassword").send_keys(pwd)
            browser.find_element(by=By.ID, value="loginButton").click()
        except NoSuchElementException as ex:
            print(f"Couldn't connect to neopets.com {type(ex).__name__}, try {self.login_attempts}")
            self.login_attempts += 1
            if self.login_attempts < 10:
                return self.login_selenium(usr, pwd)
            else:
                self.bot_status = Flag.QUIT
                return False
        try:
            WebDriverWait(browser, 20).until(EC.title_is("Welcome to Neopets!"))
        except TimeoutException as ex:
            print(f"Couldn't connect to neopets.com after {self.login_attempts} attempts")
            return False
        self.selenium_cookies = browser.get_cookies()
        for cookie in self.selenium_cookies:
            self.scraper.cookies.set(cookie['name'], cookie['value'])
        self.login_attempts = 0
        self.bot_status = Flag.OK
        browser.quit()
        return True

    def login_cloudscraper(self, usr, pwd):
        if self.login_attempts > 1:
            self.bot_status = Flag.QUIT
            return False
        print(f"Logging in neopets")
        data = {"mfa-check": None, "auth[]": None, "auth[]": None, "auth[]": None, "auth[]": None, "auth[]": None,
                "auth[]": None, "backup[]": None, "backup[]": None, "backup[]": None, "backup[]": None,
                "backup[]": None, "backup[]": None, "backup[]": None, "backup[]": None, "dob-check": None,
                "destination": None, "return_format": "json", "_ref_ck": "126479724b4db61d5fdd880523e38506",
                "username": usr, "password": pwd}
        res = self.scraper.post(url="https://www.neopets.com/login.phtml",
                                data=data,
                                headers={"X-Requested-With": "XMLHttpRequest"})
        print(res.text)
        try:
            json.loads(res.text)
        except JSONDecodeError:
            time.sleep(2)
            self.login_attempts += 1
            print(f"Couldn't connect to neopets.com after {self.login_attempts} attempts")
            return self.login_cloudscraper(usr, pwd)
        self.login_attempts = 0
        self.bot_status = Flag.OK
        return True

    def get_new_kad(self):
        res = False
        try:
            page = self.scraper.get(url=self.kad_url, timeout=36)
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
            print(f"Connection error for {self.kad_url}")
            return res
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
            print(f"Request timeout for {self.kad_url}")
            return res

        # look for kads links (https://www.neopets.com/games/kadoatery/feed_kadoatie.phtml?kad_id=2718691)
        soup = BeautifulSoup(page.content, "html.parser")
        kads = soup.find_all("a", {"href": re.compile(r'feed_.*')})
        # if kads aren't found, we've been logged out
        if kads == []:
            print("Couldn't find any kads")
            if not self.login_neopets(self.usr, self.pwd):
                return False
            else:
                return self.get_new_kad()
        # get the largest kad id (all kads ids are incrementally generated). If new high, then a refresh occurred
        latest_kad = max(list(map(lambda x: int(x.get("href").split("=")[-1]), kads)))
        if self.current_kad != 0 and latest_kad > self.current_kad:
            print("It refreshed!")
            res = True
        self.current_kad = latest_kad
        return res

    def login_neopets(self, usr, pwd):
        # return self.login_cloudscraper(usr, pwd)
        return self.login_selenium(usr, pwd)

    @tasks.loop()
    async def check_for_refresh_bot(self):
        tz = timezone('US/Pacific')  # neopets time
        if self.bot_status == Flag.OK:
            if self.count % 10 == 0:
                new_time = time.time()
                print(f"count: {self.count} | time: {new_time - self.start_time:.2f}s | last: {self.current_kad}")
                self.start_time = new_time
            self.count += 1
            if self.get_new_kad():
                estimate = datetime.now(tz) + timedelta(minutes=28)
                channel = self.get_channel(self.channel)  # discord channel ID goes here
                await channel.send(f"@everyone {self.kad_url}\n\nNext: {estimate.strftime('%I:%M %p')}\nAlternate: {(estimate + timedelta(minutes = 7)).strftime('%I:%M %p')} | {(estimate + timedelta(minutes = 14)).strftime('%I:%M %p')} | {(estimate + timedelta(minutes = 21)).strftime('%I:%M %p')} | {(estimate + timedelta(minutes = 28)).strftime('%I:%M %p')} | {(estimate + timedelta(minutes = 35)).strftime('%I:%M %p')}")

    @check_for_refresh_bot.before_loop
    async def wait_for_bot(self):
        await self.wait_until_ready()

    def check_for_refresh_local(self):
        count = 0
        while self.bot_status != Flag.QUIT:
            self.login_neopets(self.usr, self.pwd)
            print("Starting to watch!")
            while self.bot_status == Flag.OK:
                try:
                    if count % 10 == 0:
                        new_time = time.time()
                        print(f"count: {count} | time: {new_time - self.start_time:.2f}s | last: {self.current_kad}")
                        self.start_time = new_time
                    count += 1
                    if self.get_new_kad():
                        print(f"New kad! {self.kad_url}")
                        #webbrowser.open(self.kad_url, new=2)
                except KeyboardInterrupt:
                    print("Quitting...")
                    self.bot_status = Flag.STOP


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Check the Kadoatery for refreshes")
    required = parser.add_argument_group('Required arguments')
    required.add_argument('-u', '--username', help='Neopets username', required=True)
    required.add_argument('-p', '--password', help='Neopets password', required=True)
    optional = parser.add_argument_group('Optional arguments')
    optional.add_argument('-t', '--token', help='Discord token (if run as a bot)', required=False, default='')
    optional.add_argument('-c', '--channel', type=int,
                          help='Discord channel (if run as a bot)', required=False, default=-1)
    args = parser.parse_args()

    print(f"Press {COMBINATION} to stop the bot")
    bot = KadWatcher(args.username, args.password)

    if args.token != '' and args.channel != -1:
        print("Running as a discord bot!")
        bot.set_channel(args.channel)
        bot.run(args.token)
    else:
        print("Running locally!")
        bot.check_for_refresh_local()
