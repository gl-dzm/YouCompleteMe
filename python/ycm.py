#!/usr/bin/env python
#
# Copyright (C) 2011, 2012  Strahinja Val Markovic  <val@markovic.io>
#
# This file is part of YouCompleteMe.
#
# YouCompleteMe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# YouCompleteMe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with YouCompleteMe.  If not, see <http://www.gnu.org/licenses/>.

import vim
import indexer
import abc
import imp
import string
import random
import os

MIN_NUM_CHARS = int( vim.eval( "g:ycm_min_num_of_chars_for_completion" ) )
CLANG_COMPLETION_ENABLED = int( vim.eval( "g:ycm_clang_completion_enabled" ) )
CLANG_FILETYPES = set( [ 'c', 'cpp', 'objc', 'objcpp' ] )
MAX_IDENTIFIER_COMPLETIONS_RETURNED = 10
CLANG_OPTIONS_FILENAME = '.ycm_clang_options'


class Completer( object ):
  __metaclass__ = abc.ABCMeta

  def __init__( self ):
    self.future = None

  def AsyncCandidateRequestReady( self ):
    if not self.future:
      # We return True so that the caller can extract the default value from the
      # future
      return True
    return self.future.ResultsReady()


  def CandidatesFromStoredRequest( self ):
    if not self.future:
      return []
    return self.future.GetResults()

  @abc.abstractmethod
  def OnFileReadyToParse( self ):
    pass


class IdentifierCompleter( Completer ):
  def __init__( self ):
    self.completer = indexer.IdentifierCompleter()
    self.completer.EnableThreading()


  def CandidatesForQueryAsync( self, query ):
    filetype = vim.eval( "&filetype" )
    self.future = self.completer.CandidatesForQueryAndTypeAsync(
      SanitizeQuery( query ),
      filetype )


  def AddIdentifier( self, identifier ):
    filetype = vim.eval( "&filetype" )
    filepath = vim.eval( "expand('%:p')" )

    if not filetype or not filepath or not identifier:
      return

    vector = indexer.StringVec()
    vector.append( identifier )
    self.completer.AddCandidatesToDatabase( vector,
                                            filetype,
                                            filepath )


  def AddPreviousIdentifier( self ):
    self.AddIdentifier( PreviousIdentifier() )


  def AddIdentifierUnderCursor( self ):
    cursor_identifier = vim.eval( 'expand("<cword>")' )
    if not cursor_identifier:
      return

    stripped_cursor_identifier = ''.join( ( x for x in
                                            cursor_identifier if
                                            IsIdentifierChar( x ) ) )
    if not stripped_cursor_identifier:
      return

    self.AddIdentifier( stripped_cursor_identifier )


  def AddBufferIdentifiers( self ):
    filetype = vim.eval( "&filetype" )
    filepath = vim.eval( "expand('%:p')" )

    if not filetype or not filepath:
      return

    text = "\n".join( vim.current.buffer )
    self.completer.AddCandidatesToDatabaseFromBufferAsync( text,
                                                           filetype,
                                                           filepath )


  def OnFileReadyToParse( self ):
    self.AddBufferIdentifiers()


  def CandidatesFromStoredRequest( self ):
    if not self.future:
      return []
    completions = self.future.GetResults()[
      : MAX_IDENTIFIER_COMPLETIONS_RETURNED ]

    # We will never have duplicates in completions so with 'dup':1 we tell Vim
    # to add this candidate even if it's a duplicate of an existing one (which
    # will never happen). This saves us some expensive string matching
    # operations in Vim.
    return [ { 'word': x, 'dup': 1 } for x in completions ]


class ClangCompleter( Completer ):
  def __init__( self ):
    self.completer = indexer.ClangCompleter()
    self.completer.EnableThreading()
    self.contents_holder = []
    self.filename_holder = []
    self.last_diagnostics = []
    self.possibly_new_diagnostics = False
    self.flags = Flags()


  def GetUnsavedFilesVector( self ):
    files = indexer.UnsavedFileVec()
    self.contents_holder = []
    self.filename_holder = []
    for buffer in GetUnsavedBuffers():
      if not ClangAvailableForBuffer( buffer ):
        continue
      contents = '\n'.join( buffer )
      name = buffer.name
      if not contents or not name:
        continue
      self.contents_holder.append( contents )
      self.filename_holder.append( name )

      unsaved_file = indexer.UnsavedFile()
      unsaved_file.contents_ = self.contents_holder[ -1 ]
      unsaved_file.length_ = len( self.contents_holder[ -1 ] )
      unsaved_file.filename_ = self.filename_holder[ -1 ]

      files.append( unsaved_file )

    return files


  def CandidatesForQueryAsync( self, query ):
    if self.completer.UpdatingTranslationUnit():
      PostVimMessage( 'Still parsing file, no completions yet.' )
      self.future = None
      return

    # TODO: sanitize query

    # CAREFUL HERE! For UnsavedFile filename and contents we are referring
    # directly to Python-allocated and -managed memory since we are accepting
    # pointers to data members of python objects. We need to ensure that those
    # objects outlive our UnsavedFile objects. This is why we need the
    # contents_holder and filename_holder lists, to make sure the string objects
    # are still around when we call CandidatesForQueryAndLocationInFile.  We do
    # this to avoid an extra copy of the entire file contents.

    files = indexer.UnsavedFileVec()
    if not query:
      files = self.GetUnsavedFilesVector()

    line, _ = vim.current.window.cursor
    column = int( vim.eval( "s:completion_start_column" ) ) + 1
    current_buffer = vim.current.buffer
    self.future = self.completer.CandidatesForQueryAndLocationInFileAsync(
      query,
      current_buffer.name,
      line,
      column,
      files,
      self.flags.FlagsForFile( current_buffer.name ) )


  def CandidatesFromStoredRequest( self ):
    if not self.future:
      return []
    results = [ CompletionDataToDict( x ) for x in self.future.GetResults() ]
    if not results:
      PostVimMessage( 'No completions found; errors in the file?' )
    return results


  def OnFileReadyToParse( self ):
    if NumLinesInBuffer( vim.current.buffer ) < 5:
      return

    self.possibly_new_diagnostics = True

    filename = vim.current.buffer.name
    self.completer.UpdateTranslationUnitAsync(
      filename,
      self.GetUnsavedFilesVector(),
      self.flags.FlagsForFile( filename ) )


  def DiagnosticsForCurrentFileReady( self ):
    return ( self.possibly_new_diagnostics and not
             self.completer.UpdatingTranslationUnit() )


  def GetDiagnosticsForCurrentFile( self ):
    if self.DiagnosticsForCurrentFileReady():
      self.last_diagnostics = [ DiagnosticToDict( x ) for x in
                                self.completer.DiagnosticsForFile(
                                  vim.current.buffer.name ) ]
      self.possibly_new_diagnostics = False
    return self.last_diagnostics


class Flags( object ):
  def __init__( self ):
    # It's caches all the way down...
    self.flags_for_file = {}
    self.flags_module_for_file = {}
    self.flags_module_for_flags_module_file = {}


  def FlagsForFile( self, filename ):
    try:
      return self.flags_for_file[ filename ]
    except KeyError:
      flags_module = self.FlagsModuleForFile( filename )
      if not flags_module:
        return indexer.StringVec()

      results = flags_module.FlagsForFile( filename )
      sanitized_flags = SanitizeFlags( results[ 'flags' ] )

      if results[ 'do_cache' ]:
        self.flags_for_file[ filename ] = sanitized_flags
      return sanitized_flags


  def FlagsModuleForFile( self, filename ):
    try:
      return self.flags_module_for_file[ filename ]
    except KeyError:
      flags_module_file = FlagsModuleSourceFileForFile( filename )
      if not flags_module_file:
        return None

      try:
        flags_module = self.flags_module_for_flags_module_file[
          flags_module_file ]
      except KeyError:
        flags_module = imp.load_source( RandomName(), flags_module_file )
        self.flags_module_for_flags_module_file[
          flags_module_file ] = flags_module

      self.flags_module_for_file[ filename ] = flags_module
      return flags_module


def FlagsModuleSourceFileForFile( filename ):
  parent_folder = os.path.dirname( filename )
  old_parent_folder = ''

  while True:
    current_file = os.path.join( parent_folder, CLANG_OPTIONS_FILENAME )
    if os.path.exists( current_file ):
      return current_file

    old_parent_folder = parent_folder
    parent_folder = os.path.dirname( parent_folder )

    if parent_folder == old_parent_folder:
      return None



def RandomName():
  return ''.join( random.choice( string.ascii_lowercase ) for x in range( 15 ) )


def SanitizeFlags( flags ):
  sanitized_flags = []
  saw_arch = False
  for i, flag in enumerate( flags ):
    if flag == '-arch':
      saw_arch = True
      continue
    elif flag.startswith( '-arch' ):
      continue
    elif saw_arch:
      saw_arch = False
      continue

    sanitized_flags.append( flag )

  vector = indexer.StringVec()
  for flag in sanitized_flags:
    vector.append( flag )
  return vector


def NumLinesInBuffer( buffer ):
  # This is actually less than obvious, that's why it's wrapped in a function
  return len( buffer )

def PostVimMessage( message ):
  # TODO: escape the message string before formating it
  vim.command( 'echohl WarningMsg | echomsg "{0}" | echohl None'
               .format( message ) )


def GetUnsavedBuffers():
  def BufferModified( buffer_number ):
    to_eval = 'getbufvar({0}, "&mod")'.format( buffer_number )
    return bool( int( vim.eval( to_eval ) ) )

  return ( x for x in vim.buffers if BufferModified( x.number ) )


def CompletionDataToDict( completion_data ):
  # see :h complete-items for a description of the dictionary fields
  return {
    'word' : completion_data.TextToInsertInBuffer(),
    'abbr' : completion_data.original_string_,
    'menu' : completion_data.extra_menu_info_,
    'kind' : completion_data.kind_,
    'dup'  : 1,
    # TODO: add detailed_info_ as 'info'
  }


def DiagnosticToDict( diagnostic ):
  # see :h getqflist for a description of the dictionary fields
  return {
    'bufnr' : int( vim.eval( "bufnr('{0}', 1)".format(
      diagnostic.filename_ ) ) ),
    'lnum'  : diagnostic.line_number_,
    'col'   : diagnostic.column_number_,
    'text'  : diagnostic.text_,
    'type'  : diagnostic.kind_,
    'valid' : 1
  }


def CurrentColumn():
  """Do NOT access the CurrentColumn in vim.current.line. It doesn't exist yet.
  Only the chars before the current column exist in vim.current.line."""

  # vim's columns are 1-based while vim.current.line columns are 0-based
  # ... but vim.current.window.cursor (which returns a (line, column) tuple)
  # columns are 0-based, while the line from that same tuple is 1-based.
  # vim.buffers buffer objects OTOH have 0-based lines and columns.
  # Pigs have wings and I'm a loopy purple duck. Everything makes sense now.
  return vim.current.window.cursor[ 1 ]


def CurrentLineAndColumn():
  # See the comment in CurrentColumn about the calculation for the line and
  # column number
  line, column = vim.current.window.cursor
  line -= 1
  return line, column


def ClangAvailableForBuffer( buffer_object ):
  filetype = vim.eval( 'getbufvar({0}, "&ft")'.format( buffer_object.number ) )
  return filetype in CLANG_FILETYPES


def ClangAvailableForFile():
  filetype = vim.eval( "&filetype" )
  return filetype in CLANG_FILETYPES


def ShouldUseClang( start_column ):
  if not CLANG_COMPLETION_ENABLED or not ClangAvailableForFile():
    return False

  line = vim.current.line
  previous_char_index = start_column - 1
  if ( not len( line ) or
       previous_char_index < 0 or
       previous_char_index >= len( line ) ):
    return False

  if line[ previous_char_index ] == '.':
    return True

  if previous_char_index - 1 < 0:
    return False

  two_previous_chars = line[ previous_char_index - 1 : start_column ]
  if ( two_previous_chars == '->' or two_previous_chars == '::' ):
    return True

  return False


def IsIdentifierChar( char ):
  return char.isalnum() or char == '_'


def CompletionStartColumn():
  """Returns the 0-based index where the completion string should start. So if
  the user enters:
    foo.bar^
  with the cursor being at the location of the caret, then the starting column
  would be the index of the letter 'b'.
  """

  line = vim.current.line
  start_column = CurrentColumn()

  while start_column > 0 and IsIdentifierChar( line[ start_column - 1 ] ):
    start_column -= 1
  return start_column


def EscapeForVim( text ):
  return text.replace( "'", "''" )


def PreviousIdentifier():
  line_num, column_num = CurrentLineAndColumn()
  buffer = vim.current.buffer
  line = buffer[ line_num ]

  end_column = column_num

  while end_column > 0 and not IsIdentifierChar( line[ end_column - 1 ] ):
    end_column -= 1

  # Look at the previous line if we reached the end of the current one
  if end_column == 0:
    try:
      line = buffer[ line_num - 1]
    except:
      return ""
    end_column = len( line )
    while end_column > 0 and not IsIdentifierChar( line[ end_column - 1 ] ):
      end_column -= 1
    print end_column, line

  start_column = end_column
  while start_column > 0 and IsIdentifierChar( line[ start_column - 1 ] ):
    start_column -= 1

  if end_column - start_column < MIN_NUM_CHARS:
    return ""

  return line[ start_column : end_column ]


def CurrentIdentifierFinished():
  current_column = CurrentColumn()
  previous_char_index = current_column - 1
  if previous_char_index < 0:
    return True
  line = vim.current.line
  try:
    previous_char = line[ previous_char_index ]
  except IndexError:
    return False

  if IsIdentifierChar( previous_char ):
    return False

  if ( not IsIdentifierChar( previous_char ) and
       previous_char_index > 0 and
       IsIdentifierChar( line[ previous_char_index - 1 ] ) ):
    return True
  else:
    return line[ : current_column ].isspace()


def SanitizeQuery( query ):
  return query.strip()
