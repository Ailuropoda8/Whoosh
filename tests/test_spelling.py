import unittest

from whoosh import index, spelling
from whoosh.filedb.filestore import RamStorage


class TestSpelling(unittest.TestCase):
    def test_spelling(self):
        st = RamStorage()
        
        sp = spelling.SpellChecker(st, mingram=2)
        
        wordlist = ["render", "animation", "animate", "shader",
                    "shading", "zebra", "koala", "lamppost",
                    "ready", "kismet", "reaction", "page",
                    "delete", "quick", "brown", "fox", "jumped",
                    "over", "lazy", "dog", "wicked", "erase",
                    "red", "team", "yellow", "under", "interest",
                    "open", "print", "acrid", "sear", "deaf",
                    "feed", "grow", "heal", "jolly", "kilt",
                    "low", "zone", "xylophone", "crown",
                    "vale", "brown", "neat", "meat", "reduction",
                    "blunder", "preaction"]
        
        sp.add_words([unicode(w) for w in wordlist])
        
        sugs = sp.suggest(u"reoction")
        self.assertNotEqual(len(sugs), 0)
        self.assertEqual(sugs, [u"reaction", u"reduction", u"preaction"])


if __name__ == '__main__':
    unittest.main()
    print 10 + 20